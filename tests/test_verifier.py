"""Tests for the verifier — the only module permitted to approve a match.

The point of these is that the verifier trusts **no** proposer, including the deterministic ones
that cannot lie. If it only rejected malformed input it would be a validator; what makes it a
verifier is that it recomputes the arithmetic and disagrees with a layer that got it wrong.
"""

from __future__ import annotations

from core import verifier
from core.records import BankRow, GatewayRow
from core.results import EX_VERIFIER_REJECTED, GroupProposal, MatchGroup, ReconException


def gateway(row_id: str, credit: int, *, fee: int = 0, gst: int = 0) -> GatewayRow:
    return GatewayRow(
        row_id=row_id,
        type="payment",
        entity_id=f"pay_{row_id}",
        debit_paise=0,
        credit_paise=credit,
        fee_base_paise=fee,
        gst_paise=gst,
        currency="INR",
        created_at_utc=1_771_900_000,
    )


def bank(row_id: str, credit: int) -> BankRow:
    return BankRow(
        row_id=row_id,
        value_date_ist="2026-03-03",
        narration="NEFT-RAZORPAYSOFTWARE-UTRx-STL",
        reference="utrx",
        credit_paise=credit,
        debit_paise=0,
    )


def proposal(*, gateway_ids: tuple[str, ...], bank_id: str | None, records=None) -> GroupProposal:
    return GroupProposal(
        group_id="grp_test",
        layer=1,
        record_ids=records if records is not None else gateway_ids + ((bank_id,) if bank_id else ()),
        settlement_id="setl_test",
        bank_row_id=bank_id,
        gateway_row_ids=gateway_ids,
    )


def verify(prop, rows, banks):
    return verifier.verify(
        prop,
        gateway_by_id={r.row_id: r for r in rows},
        bank_by_id={r.row_id: r for r in banks},
    )


def test_a_balanced_proposal_is_approved() -> None:
    rows = [gateway("gw_1", 10_000, fee=200, gst=36), gateway("gw_2", 5_000, fee=100, gst=18)]
    credit = sum(r.net_paise for r in rows)

    verdict = verify(proposal(gateway_ids=("gw_1", "gw_2"), bank_id="bk_1"), rows, [bank("bk_1", credit)])

    assert isinstance(verdict, MatchGroup)
    assert verdict.delta_paise == 0
    assert verdict.expected_credit_paise == credit


def test_a_proposal_off_by_one_paisa_is_rejected() -> None:
    """Zero tolerance, and it is not configurable: a knob that can only cause harm."""
    rows = [gateway("gw_1", 10_000, fee=200, gst=36)]
    credit = rows[0].net_paise + 1

    verdict = verify(proposal(gateway_ids=("gw_1",), bank_id="bk_1"), rows, [bank("bk_1", credit)])

    assert isinstance(verdict, ReconException)
    assert verdict.exception_type == EX_VERIFIER_REJECTED
    assert "delta 1 paise" in verdict.detail


def test_the_verifier_does_not_trust_a_layer_that_miscalculated() -> None:
    """The behaviour that makes it a verifier rather than a validator.

    The proposal is perfectly well formed and a layer confidently claims it reconciles. The
    verifier recomputes from the records and disagrees.
    """
    rows = [gateway("gw_1", 10_000, fee=200, gst=36), gateway("gw_2", 5_000, fee=100, gst=18)]
    # A layer that forgot to subtract GST would propose this credit.
    wrong = sum(r.credit_paise - r.fee_base_paise for r in rows)

    verdict = verify(proposal(gateway_ids=("gw_1", "gw_2"), bank_id="bk_1"), rows, [bank("bk_1", wrong)])

    assert isinstance(verdict, ReconException)
    assert "does not balance" in verdict.detail


def test_a_proposal_naming_a_nonexistent_row_is_rejected() -> None:
    """A hallucinated record id cannot enter the ledger — the Phase 5 guarantee, tested now."""
    verdict = verify(
        proposal(gateway_ids=("gw_1", "gw_ghost"), bank_id="bk_1"),
        [gateway("gw_1", 10_000)],
        [bank("bk_1", 10_000)],
    )

    assert isinstance(verdict, ReconException)
    assert "do not exist" in verdict.detail


def test_a_proposal_with_no_bank_row_is_rejected() -> None:
    verdict = verify(proposal(gateway_ids=("gw_1",), bank_id=None), [gateway("gw_1", 10_000)], [])

    assert isinstance(verdict, ReconException)
    assert "no bank row" in verdict.detail


def test_a_proposal_naming_a_nonexistent_bank_row_is_rejected() -> None:
    verdict = verify(proposal(gateway_ids=("gw_1",), bank_id="bk_ghost"), [gateway("gw_1", 10_000)], [])

    assert isinstance(verdict, ReconException)
    assert "does not exist" in verdict.detail


def test_duplicate_record_ids_are_rejected() -> None:
    """A group that double-counts a record would make the partition invariant measure a
    multiset rather than a set."""
    rows = [gateway("gw_1", 10_000)]
    verdict = verify(
        proposal(gateway_ids=("gw_1",), bank_id="bk_1", records=("gw_1", "gw_1", "bk_1")),
        rows,
        [bank("bk_1", 10_000)],
    )

    assert isinstance(verdict, ReconException)
    assert "duplicate record ids" in verdict.detail


def test_gst_is_summed_from_rows_not_recomputed() -> None:
    """The verifier must inherit the summation rule, or it would reject valid groups.

    Two rows with fee_base 25 have per-row GST of 5 each, totalling 10. Recomputing from the
    summed fee base of 50 gives 9, so a verifier that recomputed would compute an expected
    credit one paisa too high and reject a batch that balances.
    """
    rows = [gateway("gw_1", 100_000, fee=25, gst=5), gateway("gw_2", 100_000, fee=25, gst=5)]
    credit = sum(r.net_paise for r in rows)
    assert credit == 200_000 - 50 - 10

    verdict = verify(proposal(gateway_ids=("gw_1", "gw_2"), bank_id="bk_1"), rows, [bank("bk_1", credit)])

    assert isinstance(verdict, MatchGroup), (
        "the verifier recomputed GST from the summed fee base and rejected a balancing batch"
    )


def test_the_rejection_carries_the_layer_that_proposed_it() -> None:
    """An exception that cannot name its proposer is not auditable."""
    verdict = verify(proposal(gateway_ids=("gw_1",), bank_id="bk_1"), [gateway("gw_1", 10_000)], [bank("bk_1", 99)])

    assert isinstance(verdict, ReconException)
    assert verdict.layer == 1
    assert "layer 1 proposal grp_test" in verdict.detail
