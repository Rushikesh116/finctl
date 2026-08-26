"""The only module permitted to approve a match.

Every layer *proposes*; this module disposes. It recomputes the settlement identity from the
records themselves — never from whatever the proposing layer calculated — and approves only if
the batch balances at exactly zero. A proposal that does not balance becomes a
`VERIFIER_REJECTED` exception.

Invariant 3 is usually described as an LLM guardrail, and it is one: a hallucinated match cannot
enter the ledger, and prompt injection through untrusted bank narration cannot cause a false
match, because at worst it produces a proposal that fails arithmetic it cannot influence.

But the boundary is built here, in Phase 3, rather than alongside the LLM in Phase 5,
deliberately. A verifier retrofitted after two layers already approve directly is a verifier
with holes in it — the holes being exactly the paths that predate it. Routing the deterministic
layers through it first means the LLM path in Phase 5 is just another proposer, with no new
trust granted and no special case to audit.

The verifier therefore trusts **no** proposer, including the ones that cannot lie.
"""

from __future__ import annotations

from core.money import Paise
from core.normalize import expected_credit_paise
from core.records import BankRow, GatewayRow
from core.results import EX_VERIFIER_REJECTED, GroupProposal, MatchGroup, ReconException

__all__ = ["ZERO_TOLERANCE_PAISE", "verify"]

ZERO_TOLERANCE_PAISE = 0
"""Money is exact. A non-zero tolerance is the mechanism by which false matches enter a ledger
while the headline match rate improves — so the tolerance is not configurable, because a knob
that can only cause harm should not exist."""


def verify(
    proposal: GroupProposal,
    *,
    gateway_by_id: dict[str, GatewayRow],
    bank_by_id: dict[str, BankRow],
) -> MatchGroup | ReconException:
    """Approve a proposal, or convert it into a `VERIFIER_REJECTED` exception.

    Recomputes both sides independently:

    * expected credit, as `Σ credit − Σ debit − Σ fee_base − Σ gst` over the proposal's
      gateway rows (`Σ gst` summed from stored per-row values, never recomputed from the
      summed fee base — half-up rounding does not distribute over addition);
    * actual credit, read from the bank row the proposal names.

    Nothing the proposing layer computed is reused, so a layer that miscalculates its own δ
    cannot get a group approved on the strength of its own mistake.
    """
    missing = [row_id for row_id in proposal.gateway_row_ids if row_id not in gateway_by_id]
    if missing:
        return _reject(
            proposal,
            0,
            f"proposal names {len(missing)} gateway rows that do not exist: {missing[:5]}",
        )

    rows = [gateway_by_id[row_id] for row_id in proposal.gateway_row_ids]
    expected: Paise = expected_credit_paise(rows)

    if proposal.bank_row_id is None:
        return _reject(
            proposal,
            expected,
            "proposal names no bank row, so there is no actual credit to verify against",
        )
    if proposal.bank_row_id not in bank_by_id:
        return _reject(
            proposal, expected, f"proposal names bank row {proposal.bank_row_id} which does not exist"
        )

    actual: Paise = bank_by_id[proposal.bank_row_id].credit_paise
    delta = actual - expected

    if delta != ZERO_TOLERANCE_PAISE:
        return _reject(
            proposal,
            expected,
            f"identity does not balance: expected {expected}, actual {actual}, delta {delta} "
            "paise. The proposing layer claimed this group reconciles; it does not.",
        )

    # Every record in the proposal must be distinct, or the group double-counts a record and
    # the partition invariant downstream would be measuring a multiset.
    if len(set(proposal.record_ids)) != len(proposal.record_ids):
        return _reject(proposal, expected, "proposal contains duplicate record ids")

    return MatchGroup(
        group_id=proposal.group_id,
        layer=proposal.layer,
        record_ids=proposal.record_ids,
        settlement_id=proposal.settlement_id,
        bank_row_id=proposal.bank_row_id,
        expected_credit_paise=expected,
        actual_credit_paise=actual,
    )


def _reject(proposal: GroupProposal, expected: Paise, reason: str) -> ReconException:
    return ReconException(
        exception_type=EX_VERIFIER_REJECTED,
        layer=proposal.layer,
        record_ids=proposal.record_ids,
        amount_at_risk_paise=abs(expected),
        detail=f"layer {proposal.layer} proposal {proposal.group_id} rejected: {reason}",
    )
