"""Layer 1 — exact matching, and nothing more.

Groups gateway rows into batches by `settlement_id`, joins each batch to its bank credit by
`settlement_utr` == `BankRow.reference`, attaches merchant rows by `order_receipt`, and then
**verifies the settlement identity at zero tolerance** before approving anything.

That last step is what makes the baseline honest. A join is not a reconciliation: the bank
credit either equals the batch's expected credit or it does not, and a layer that approves
groups without checking would report coverage it has not earned. Batches that do not balance
are handed on, not approved.

Two refusals live here, both exact rather than fuzzy:

* A reference matching **several** bank credits is disambiguated by value date — an exact
  composite key, using the IST interval rule from `normalize`. If the dates collide too, it
  refuses with `DUPLICATE_REFERENCE` rather than picking.
* A batch with a reference and **no** credit is `MISSING_BANK_ROW` immediately. There is no
  scalar to reconstruct against, so there is nothing to search for (`SPEC.md` §4.1 M0) —
  distinguishing absence from mismatch is a design requirement, not an optimisation.
"""

from __future__ import annotations

from collections import defaultdict

from core import verifier
from core.money import Paise
from core.normalize import NormalizedDataset, expected_credit_paise, ist_date_of
from core.records import BankRow, GatewayRow, MerchantLedgerRow
from core.results import (
    EX_DUPLICATE_REFERENCE,
    EX_MISSING_BANK_ROW,
    EX_MISSING_GATEWAY_ROW,
    EX_UNCLASSIFIED,
    CandidateBatch,
    GroupProposal,
    LayerResult,
    MatchGroup,
    ReconException,
)

__all__ = ["LAYER", "resolve", "unresolved_record_ids"]

LAYER = 1


def _amount_at_risk(
    gateway: list[GatewayRow], bank: list[BankRow], merchant: list[MerchantLedgerRow]
) -> Paise:
    """The money a single exception puts at risk, counting each movement **once**.

    The gateway amount is authoritative where several sources describe the same movement;
    summing all three would triple-count and inflate the figure by roughly 3x, which a
    reviewer spots immediately.
    """
    if gateway:
        return sum(abs(row.net_paise) for row in gateway)
    if bank:
        return sum(row.credit_paise + row.debit_paise for row in bank)
    return sum(row.amount_paise for row in merchant)


def resolve(dataset: NormalizedDataset) -> LayerResult:
    """Run Layer 1 over a normalised dataset."""
    result = LayerResult()

    gateway_by_id = {row.row_id: row for row in dataset.gateway_rows}
    bank_by_id = {row.row_id: row for row in dataset.bank_rows}

    # Batches: gateway rows carrying a settlement_id. Rows without one are the unassigned
    # pool — legitimately unassigned, not missing data (SPEC §4.1).
    batches: dict[str, list[GatewayRow]] = defaultdict(list)
    pool: list[GatewayRow] = []
    for row in dataset.gateway_rows:
        if row.settlement_id:
            batches[row.settlement_id].append(row)
        else:
            pool.append(row)
    result.pool_row_ids = [row.row_id for row in pool]

    # Bank credits indexed by the reference as printed. Deliberately not deduplicated: a
    # reference colliding across days is pathology 2, and collapsing it here would hide it.
    credits_by_reference: dict[str, list[BankRow]] = defaultdict(list)
    for row in dataset.bank_rows:
        if row.credit_paise > 0 and row.reference:
            credits_by_reference[row.reference].append(row)

    merchant_by_receipt: dict[str, list[MerchantLedgerRow]] = defaultdict(list)
    for row in dataset.merchant_rows:
        merchant_by_receipt[row.order_ref].append(row)

    claimed_bank_ids: list[str] = []
    claimed_merchant_ids: list[str] = []

    for settlement_id in sorted(batches):
        members = sorted(batches[settlement_id], key=lambda r: r.row_id)
        utr = next((row.settlement_utr for row in members if row.settlement_utr), None)
        expected = expected_credit_paise(members)

        merchant_rows = [
            row
            for member in members
            if member.order_receipt
            for row in merchant_by_receipt.get(member.order_receipt, [])
        ]
        merchant_ids = tuple(sorted(row.row_id for row in merchant_rows))

        settled_at_for_batch = next(
            (row.settled_at_utc for row in members if row.settled_at_utc), None
        )
        matches = credits_by_reference.get(utr or "", [])

        if not matches:
            result.exceptions.append(
                ReconException(
                    exception_type=EX_MISSING_BANK_ROW,
                    layer=LAYER,
                    # Merchant rows ARE named. Releasing them to Layer 3 was tried and
                    # reverted: their gateway counterparts sit in the same unresolved batches,
                    # so Layer 3 could pair none of them, and the release moved 38 records from
                    # a specific verdict to UNCLASSIFIED. An unresolved batch is a fact about
                    # every record in it, ledger rows included.
                    record_ids=tuple(row.row_id for row in members) + merchant_ids,
                    amount_at_risk_paise=_amount_at_risk(members, [], []),
                    detail=(
                        f"{settlement_id} settled with UTR {utr or '(none)'} but no bank "
                        "credit carries that reference. Absence, not mismatch: there is no "
                        "scalar to reconstruct against, so no search is attempted."
                    ),
                )
            )
            continue

        if len(matches) > 1:
            # Exact composite key: reference plus value date. Uses the IST interval rule, so a
            # reference reused on a different day still resolves without any fuzzy matching.
            if settled_at_for_batch is not None:
                target_date = ist_date_of(settled_at_for_batch)
                narrowed = [row for row in matches if row.value_date_ist == target_date]
                if len(narrowed) == 1:
                    matches = narrowed

        if len(matches) > 1:
            result.exceptions.append(
                ReconException(
                    exception_type=EX_DUPLICATE_REFERENCE,
                    layer=LAYER,
                    record_ids=tuple(row.row_id for row in members)
                    + tuple(sorted(row.row_id for row in matches))
                    + merchant_ids,
                    amount_at_risk_paise=_amount_at_risk(members, [], []),
                    detail=(
                        f"reference {utr} matches {len(matches)} bank credits and their value "
                        "dates collide too, so nothing distinguishes them. Refused rather "
                        "than guessed."
                    ),
                )
            )
            claimed_bank_ids.extend(row.row_id for row in matches)
            continue

        credit = matches[0]
        delta = credit.credit_paise - expected
        claimed_bank_ids.append(credit.row_id)

        if delta == 0:
            # Zero tolerance. But Layer 1 does not approve its own work: it proposes, and
            # core/verifier.py recomputes the arithmetic independently before anything
            # enters the matched ledger (invariant 3).
            proposal = GroupProposal(
                group_id=f"grp_{settlement_id}",
                layer=LAYER,
                record_ids=tuple(row.row_id for row in members)
                + (credit.row_id,)
                + merchant_ids,
                settlement_id=settlement_id,
                bank_row_id=credit.row_id,
                gateway_row_ids=tuple(row.row_id for row in members),
                detail=f"exact join on {utr}, identity balanced at zero tolerance",
            )
            verdict = verifier.verify(
                proposal, gateway_by_id=gateway_by_id, bank_by_id=bank_by_id
            )
            if isinstance(verdict, ReconException):
                result.exceptions.append(verdict)
            else:
                result.groups.append(verdict)
                claimed_merchant_ids.extend(merchant_ids)
        else:
            result.candidates.append(
                CandidateBatch(
                    settlement_id=settlement_id,
                    settlement_utr=utr,
                    bank_row_id=credit.row_id,
                    delta_paise=delta,
                    member_row_ids=tuple(row.row_id for row in members),
                    merchant_row_ids=merchant_ids,
                    settled_at_utc=settled_at_for_batch,
                    actual_credit_paise=credit.credit_paise,
                )
            )

    # Bank credits nothing claimed. A credit with no gateway batch behind it is the mirror of
    # pathology 8 and equally an absence rather than a mismatch.
    for row in sorted(dataset.bank_rows, key=lambda r: r.row_id):
        if row.row_id in claimed_bank_ids or row.credit_paise <= 0:
            continue
        result.exceptions.append(
            ReconException(
                exception_type=EX_MISSING_GATEWAY_ROW,
                layer=LAYER,
                record_ids=(row.row_id,),
                amount_at_risk_paise=_amount_at_risk([], [row], []),
                detail=(
                    f"bank credit {row.row_id} carries reference "
                    f"{row.reference or '(none)'} which matches no settlement. Narration: "
                    f"{row.narration[:60]!r}"
                ),
            )
        )

    return result


def unresolved_record_ids(dataset: NormalizedDataset, result: LayerResult) -> list[str]:
    """Every record Layer 1 neither grouped nor already named in an exception.

    Kept separate from `resolve` so the cascade contract stays explicit: a later layer
    receives exactly what earlier layers did not settle, and nothing is silently dropped
    between them. The harness turns whatever is still here into exceptions.
    """
    accounted: set[str] = set()
    for group in result.groups:
        accounted.update(group.record_ids)
    for exception in result.exceptions:
        accounted.update(exception.record_ids)

    everything = (
        [row.row_id for row in dataset.merchant_rows]
        + [row.row_id for row in dataset.gateway_rows]
        + [row.row_id for row in dataset.bank_rows]
    )
    return [row_id for row_id in everything if row_id not in accounted]
