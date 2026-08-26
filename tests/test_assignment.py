"""Tests for Layer 3 — candidate generation, global assignment, and the refusal rule.

Two properties carry the layer. **Arithmetic is not negotiable**: a good cost cannot carry a
mismatched amount past the verifier. And **the refusal rule is necessity, not per-row ranking**:
two rows can each have distinctly ranked candidates while two whole assignments tie on total
cost, and a per-row check would match both and be wrong twice.
"""

from __future__ import annotations

import pytest

from core import assignment, verifier
from core.normalize import NormalizedDataset
from core.records import GatewayRow, MerchantLedgerRow
from core.results import EX_AMBIGUOUS, EX_VERIFIER_REJECTED, GroupProposal, ReconException

DAY = 86_400
BASE = 1_772_000_000


def order(row_id: str, amount: int, *, at: int = BASE, gateway_order_id: str | None = None) -> MerchantLedgerRow:
    return MerchantLedgerRow(
        row_id=row_id,
        kind="order",
        order_ref=f"receipt#{row_id}",
        gateway_order_id=gateway_order_id,
        amount_paise=amount,
        currency="INR",
        minor_unit_scale=100,
        issued_at_utc=at,
        customer_ref=None,
    )


def payment(row_id: str, amount: int, *, at: int = BASE, order_id: str | None = None) -> GatewayRow:
    return GatewayRow(
        row_id=row_id,
        type="payment",
        entity_id=f"pay_{row_id}",
        debit_paise=0,
        credit_paise=amount,
        fee_base_paise=0,
        gst_paise=0,
        currency="INR",
        created_at_utc=at,
        order_id=order_id,
    )


def run(merchants, gateways, **kw):
    data = NormalizedDataset(
        merchant_rows=list(merchants), gateway_rows=list(gateways), bank_rows=[]
    )
    return assignment.resolve(
        data, [m.row_id for m in merchants], [g.row_id for g in gateways], **kw
    )


# --- the arithmetic gate -------------------------------------------------------------------


def test_amounts_must_agree_exactly_to_be_candidates() -> None:
    """One paisa apart is not a near-candidate. Tolerance is zero (D-0024)."""
    result = run([order("m1", 10_000)], [payment("g1", 10_001)])

    assert not result.groups
    assert not result.exceptions, "a non-candidate should not even produce a refusal"


def test_a_payment_cannot_precede_the_order_it_pays() -> None:
    result = run([order("m1", 10_000, at=BASE)], [payment("g1", 10_000, at=BASE - DAY)])
    assert not result.groups


def test_a_candidate_outside_the_date_window_is_not_a_candidate() -> None:
    result = run(
        [order("m1", 10_000, at=BASE)],
        [payment("g1", 10_000, at=BASE + 30 * DAY)],
        window_days=7,
    )
    assert not result.groups


def test_the_verifier_rejects_a_mismatched_amount_however_good_the_cost() -> None:
    """Cost decides what is proposed; arithmetic decides what is accepted."""
    merchant, gateway = order("m1", 10_000), payment("g1", 9_999)
    proposal = GroupProposal(
        group_id="grp_forced",
        layer=assignment.LAYER,
        record_ids=("m1", "g1"),
        settlement_id=None,
        bank_row_id=None,
        gateway_row_ids=("g1",),
    )
    verdict = verifier.verify_pairing(
        proposal,
        merchant_row_id="m1",
        merchant_by_id={"m1": merchant},
        gateway_by_id={"g1": gateway},
    )

    assert isinstance(verdict, ReconException)
    assert verdict.exception_type == EX_VERIFIER_REJECTED
    assert "amounts disagree" in verdict.detail


# --- resolving ------------------------------------------------------------------------------


def test_a_single_candidate_resolves() -> None:
    result = run([order("m1", 10_000)], [payment("g1", 10_000)])

    assert len(result.groups) == 1
    assert set(result.groups[0].record_ids) == {"m1", "g1"}
    assert result.groups[0].delta_paise == 0


def test_a_matching_order_id_outranks_date_proximity() -> None:
    """A key match is decisive evidence; date proximity is a weak signal beside it."""
    result = run(
        [order("m1", 10_000, gateway_order_id="order_7")],
        [
            payment("g_near", 10_000, at=BASE),
            payment("g_keyed", 10_000, at=BASE + 5 * DAY, order_id="order_7"),
        ],
    )

    assert len(result.groups) == 1
    assert "g_keyed" in result.groups[0].record_ids, "date proximity beat a matching order id"


# --- the refusal rule -----------------------------------------------------------------------


def test_two_interchangeable_candidates_are_refused_with_both_recorded() -> None:
    """Pathology 7. A refusal without its evidence is a claim, so both candidates are named."""
    result = run(
        [order("m1", 10_000), order("m2", 10_000)],
        [payment("g1", 10_000), payment("g2", 10_000)],
    )

    assert not result.groups, "picked between two interchangeable candidates"
    assert len(result.exceptions) == 1
    exception = result.exceptions[0]
    assert exception.exception_type == EX_AMBIGUOUS
    assert set(exception.record_ids) == {"m1", "m2", "g1", "g2"}
    assert exception.evidence_found == 4, "every candidate pairing must be recorded"
    assert exception.evidence_complete
    for evidence in exception.evidence:
        assert evidence.sum_paise == 10_000, "each recorded pairing carries the amount it satisfies"


def test_global_degeneracy_is_caught_not_just_per_row_ties() -> None:
    """The case a per-row margin check would get wrong, twice.

    m1 and m2 each rank g1 above g2 — neither row has a tie. But the two whole assignments
    (m1->g1, m2->g2) and (m1->g2, m2->g1) cost the same total, so nothing determines either
    pairing. Necessity catches this; per-row ranking does not.
    """
    result = run(
        [order("m1", 10_000, at=BASE), order("m2", 10_000, at=BASE)],
        [payment("g1", 10_000, at=BASE), payment("g2", 10_000, at=BASE + DAY)],
    )

    assert not result.groups, (
        "matched a globally degenerate assignment — a per-row tie check would do exactly this"
    )
    assert result.exceptions[0].exception_type == EX_AMBIGUOUS


def test_a_determined_pair_survives_alongside_an_ambiguous_block() -> None:
    """Ambiguity is local: one undetermined block must not block an unrelated clean pairing."""
    result = run(
        [order("m1", 10_000), order("m2", 10_000), order("m3", 55_555)],
        [payment("g1", 10_000), payment("g2", 10_000), payment("g3", 55_555)],
    )

    assert len(result.groups) == 1
    assert set(result.groups[0].record_ids) == {"m3", "g3"}
    assert len(result.exceptions) == 1
    assert set(result.exceptions[0].record_ids) == {"m1", "m2", "g1", "g2"}


def test_competing_rows_are_one_ambiguity_not_several() -> None:
    """Two rows fighting over the same two payments is one problem, reported once.

    Emitting an exception per row would double-count it and make the queue look twice as bad.
    """
    result = run(
        [order("m1", 10_000), order("m2", 10_000)],
        [payment("g1", 10_000), payment("g2", 10_000)],
    )
    assert len(result.exceptions) == 1


def test_layer_3_is_deterministic() -> None:
    merchants = [order(f"m{i}", 1_000 * (i % 4 + 1)) for i in range(12)]
    gateways = [payment(f"g{i}", 1_000 * (i % 4 + 1)) for i in range(12)]

    first, second = run(merchants, gateways), run(merchants, gateways)
    assert [g.record_ids for g in first.groups] == [g.record_ids for g in second.groups]
    assert [e.record_ids for e in first.exceptions] == [e.record_ids for e in second.exceptions]


# --- against the real datasets ---------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not __import__("data.generator", fromlist=["dataset_paths"])
    .dataset_paths("dev_seed_11")["gateway"]
    .exists(),
    reason="run `make seed` first — data/generated/ is gitignored",
)


@pytest.mark.parametrize("dataset_name", ["dev_seed_11", "holdout_seed_97"])
def test_pathology_7_is_refused_with_every_candidate_recorded(dataset_name: str) -> None:
    """Phase 4's gate: all 8 records declared AMBIGUOUS, to M5's evidence standard."""
    from eval import harness

    metrics = harness.evaluate(dataset_name)
    correct, total = metrics.refusals[harness.REFUSAL_RECORD_TIE]

    assert total == 8, f"{dataset_name}: expected 8 pathology-7 records, found {total}"
    assert correct == total, (
        f"{dataset_name}: only {correct}/{total} pathology-7 records were declared AMBIGUOUS. "
        "Not matching them is not the same as refusing them."
    )


@pytest.mark.parametrize("dataset_name", ["dev_seed_11", "holdout_seed_97"])
def test_layer_3_introduces_no_false_matches(dataset_name: str) -> None:
    """The before/after pair that justifies the layer. Attribution risk is real; measure it."""
    from eval import harness

    before = harness.evaluate(dataset_name, max_layer=2)
    after = harness.evaluate(dataset_name, max_layer=3)

    assert after.auto_matched >= before.auto_matched, "Layer 3 lost coverage"
    assert after.false_matches <= before.false_matches, (
        f"{dataset_name}: false matches rose from {before.false_matches} to "
        f"{after.false_matches}. That is not automatically a failure — Layer 3 trades "
        "attribution certainty for coverage by design — but it must be reported as a trade "
        "with both numbers, not absorbed."
    )


@pytest.mark.parametrize("dataset_name", ["dev_seed_11", "holdout_seed_97"])
def test_every_layer_3_group_has_equal_amounts(dataset_name: str) -> None:
    """The verifier's contract, checked on real output rather than trusted."""
    from core import identity, normalize, settlement
    from data.generator import dataset_paths

    paths = dataset_paths(dataset_name)
    data = normalize.load_dataset(
        merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"]
    )
    first = identity.resolve(data)
    first.merge(settlement.resolve(data, first.candidates, first.pool_row_ids))
    spoken = {i for g in first.groups for i in g.record_ids} | {
        i for e in first.exceptions for i in e.record_ids
    }
    third = assignment.resolve(
        data,
        [m.row_id for m in data.merchant_rows if m.row_id not in spoken],
        [g.row_id for g in data.gateway_rows if g.row_id not in spoken],
    )

    for group in third.groups:
        assert group.expected_credit_paise == group.actual_credit_paise
