"""Layer 3 — candidate generation and globally optimal assignment.

Pairs merchant-ledger rows to gateway payments that Layer 1's `order_receipt` join missed. Two
things make this the layer where false matches could enter, and both are constrained:

**Arithmetic is still zero-tolerance** (D-0024). A candidate must satisfy exact amount equality,
exact currency, and an order preceding its payment. `core/verifier.py` re-checks all three.
**Cost decides which candidate is proposed; it never decides whether a proposal is accepted.**
So Layer 3 cannot produce a group whose money does not add up — only one whose money adds up and
whose counterparty is wrong. That residual attribution risk is real, and the before/after
false-match rate is the instrument for it.

**Assignment is global, never greedy** (D-0002). Greedy matching starves correct pairings: taking
a locally best pair can deny two other records their only correct partner, which inflates the
false-match rate while raising the headline. `scipy.optimize.linear_sum_assignment` solves the
whole matrix at once.

**The ambiguity rule is margin zero** — refuse on an exact tie (D-0023, pre-registered before
this module existed). Testing that per row would be wrong: two rows can each have distinctly
ranked candidates while two *whole assignments* tie on total cost. So the test is necessity —
forbid an assigned pair, re-solve, and if the optimum is unchanged that pair was never
determined. It catches per-row ties and global degeneracy with one rule.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

from core import verifier
from core.money import Paise, format_rupees
from core.normalize import NormalizedDataset
from core.records import GatewayRow, MerchantLedgerRow
from core.results import (
    EX_AMBIGUOUS,
    GroupProposal,
    LayerResult,
    ReconException,
    SubsetEvidence,
)

__all__ = [
    "DEFAULT_DATE_WINDOW_DAYS",
    "KEY_MISMATCH_PENALTY",
    "LAYER",
    "resolve",
]

LAYER = 3

DEFAULT_DATE_WINDOW_DAYS = 7

# A matching order id is decisive evidence, so it dominates any amount of date proximity. The
# penalty exceeds the largest reachable date cost by construction.
KEY_MISMATCH_PENALTY = 100_000

# Cost for a pair that is not a candidate at all. Large enough that the solver never prefers it,
# and recognisable afterwards so those assignments can be discarded.
_FORBIDDEN = 10**9

_SECONDS_PER_DAY = 86_400


@dataclass(frozen=True, slots=True)
class Candidate:
    merchant_row_id: str
    gateway_row_id: str
    amount_paise: Paise
    cost: int


def _is_candidate(
    merchant: MerchantLedgerRow, gateway: GatewayRow, window_days: int
) -> bool:
    """The hard filter. Every clause is arithmetic or causality, never plausibility.

    Amount equality is **exact** — `FINCTL_AMOUNT_TOLERANCE_PAISE` is 0 by design, so two rows
    differing by a paisa are not near-candidates, they are not candidates. That is what keeps
    the fuzziness in the pairing rather than in the money.
    """
    if gateway.type != "payment":
        return False
    if merchant.kind != "order":
        return False
    if merchant.amount_paise != gateway.credit_paise:
        return False
    if merchant.currency != gateway.currency:
        return False
    # An order cannot be paid before it was placed.
    if merchant.issued_at_utc > gateway.created_at_utc:
        return False
    return gateway.created_at_utc - merchant.issued_at_utc <= window_days * _SECONDS_PER_DAY


def _cost(merchant: MerchantLedgerRow, gateway: GatewayRow) -> int:
    """Integer cost, lower is better. Integers throughout: a float cost would make "exact tie"
    a question about floating-point equality, which is not a question worth asking."""
    days = (gateway.created_at_utc - merchant.issued_at_utc) // _SECONDS_PER_DAY
    keys_agree = (
        merchant.gateway_order_id is not None
        and gateway.order_id is not None
        and merchant.gateway_order_id == gateway.order_id
    )
    return int(days) + (0 if keys_agree else KEY_MISMATCH_PENALTY)


def _solve(matrix: np.ndarray) -> tuple[list[tuple[int, int]], int]:
    """Globally optimal assignment and its total cost."""
    rows, cols = linear_sum_assignment(matrix)
    pairs = [(int(r), int(c)) for r, c in zip(rows, cols)]
    return pairs, int(matrix[rows, cols].sum())


def resolve(
    dataset: NormalizedDataset,
    unmatched_merchant_ids: list[str],
    unmatched_gateway_ids: list[str],
    *,
    window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> LayerResult:
    """Run Layer 3 over whatever Layers 1 and 2 could not settle."""
    result = LayerResult()

    merchant_by_id = {r.row_id: r for r in dataset.merchant_rows}
    gateway_by_id = {r.row_id: r for r in dataset.gateway_rows}

    merchants = [merchant_by_id[i] for i in sorted(unmatched_merchant_ids) if i in merchant_by_id]
    gateways = [gateway_by_id[i] for i in sorted(unmatched_gateway_ids) if i in gateway_by_id]
    if not merchants or not gateways:
        return result

    matrix = np.full((len(merchants), len(gateways)), _FORBIDDEN, dtype=np.int64)
    candidates: dict[int, list[Candidate]] = {}
    for i, merchant in enumerate(merchants):
        for j, gateway in enumerate(gateways):
            if not _is_candidate(merchant, gateway, window_days):
                continue
            cost = _cost(merchant, gateway)
            matrix[i, j] = cost
            candidates.setdefault(i, []).append(
                Candidate(merchant.row_id, gateway.row_id, merchant.amount_paise, cost)
            )

    if not candidates:
        return result

    assigned, optimum = _solve(matrix)
    real = [(i, j) for i, j in assigned if matrix[i, j] < _FORBIDDEN]

    # --- necessity test: is each assigned pair required for the optimum? ------------------
    # Margin zero, expressed on the global solution rather than per row. Forbidding a pair and
    # re-solving asks exactly the pre-registered question: does an equally good alternative
    # exist? If it does, nothing in the data determined this pairing.
    determined: list[tuple[int, int]] = []
    ambiguous_rows: list[int] = []
    for i, j in real:
        original = matrix[i, j]
        matrix[i, j] = _FORBIDDEN
        _, alternative = _solve(matrix)
        matrix[i, j] = original
        if alternative == optimum:
            ambiguous_rows.append(i)
        else:
            determined.append((i, j))

    # --- propose the determined pairs -----------------------------------------------------
    for i, j in determined:
        merchant, gateway = merchants[i], gateways[j]
        proposal = GroupProposal(
            group_id=f"grp_l3_{merchant.row_id}",
            layer=LAYER,
            record_ids=(merchant.row_id, gateway.row_id),
            settlement_id=None,
            bank_row_id=None,
            gateway_row_ids=(gateway.row_id,),
            detail=(
                f"uniquely optimal pairing at cost {int(matrix[i, j])}; forbidding it worsens "
                "the global optimum, so no equally good alternative exists"
            ),
        )
        verdict = verifier.verify_pairing(
            proposal,
            merchant_row_id=merchant.row_id,
            merchant_by_id=merchant_by_id,
            gateway_by_id=gateway_by_id,
        )
        if isinstance(verdict, ReconException):
            result.exceptions.append(verdict)
        else:
            result.groups.append(verdict)

    # --- refuse the undetermined ones, with their evidence ---------------------------------
    # Grouped into blocks: rows that compete for the same gateway rows are one ambiguity, not
    # several, and reporting them separately would overstate how many distinct problems exist.
    blocks = _group_ambiguities(ambiguous_rows, candidates, optimum, matrix)
    for row_indices, gateway_indices in blocks:
        involved_merchants = [merchants[i] for i in sorted(row_indices)]
        involved_gateways = [gateways[j] for j in sorted(gateway_indices)]

        # Evidence: every candidate pairing in the block, each recorded with the amount it
        # satisfies. Same standard as an M5 subset refusal — a refusal without its evidence is
        # a claim, so a reader can check each pairing without querying anything.
        evidence = tuple(
            SubsetEvidence(
                row_ids=(candidate.merchant_row_id, candidate.gateway_row_id),
                sum_paise=candidate.amount_paise,
            )
            for i in sorted(row_indices)
            for candidate in sorted(
                candidates.get(i, []), key=lambda c: (c.cost, c.gateway_row_id)
            )
        )
        result.exceptions.append(
            ReconException(
                exception_type=EX_AMBIGUOUS,
                layer=LAYER,
                record_ids=tuple(r.row_id for r in involved_merchants)
                + tuple(r.row_id for r in involved_gateways),
                amount_at_risk_paise=sum(r.amount_paise for r in involved_merchants),
                detail=(
                    f"{len(involved_merchants)} ledger rows and {len(involved_gateways)} "
                    f"gateway payments of {format_rupees(involved_merchants[0].amount_paise, prefix='Rs ')} "
                    "are mutually interchangeable: every candidate pairing is equally optimal, "
                    "so nothing in the data says which payment belongs to which order. The "
                    "money reconciles either way; the attribution does not, and attributing a "
                    "payment to the wrong customer is a wrong statement about that customer's "
                    "account. Refused rather than picked."
                ),
                evidence=evidence,
                evidence_found=len(evidence),
                evidence_truncated=False,
                evidence_complete=True,
            )
        )

    return result


def _group_ambiguities(
    ambiguous_rows: list[int],
    candidates: dict[int, list[Candidate]],
    optimum: int,
    matrix: np.ndarray,
) -> list[tuple[set[int], set[int]]]:
    """Merge ambiguous rows that compete for the same gateway rows into single blocks.

    Two ledger rows fighting over the same two payments are *one* ambiguity. Emitting an
    exception per row would double-count the problem and make the queue look twice as bad as it
    is.
    """
    blocks: list[tuple[set[int], set[int]]] = []
    for i in sorted(ambiguous_rows):
        mine = {c.gateway_row_id for c in candidates.get(i, [])}
        indices = {
            j for j in range(matrix.shape[1]) if matrix[i, j] < _FORBIDDEN
        }
        merged = False
        for rows, cols in blocks:
            if cols & indices:
                rows.add(i)
                cols.update(indices)
                merged = True
                break
        if not merged:
            blocks.append(({i}, set(indices)))
    return blocks
