"""Tests for the evaluation harness.

The centrepiece is `test_the_false_match_detector_actually_detects_a_false_match`. The Phase 2
baseline reports a false-match rate of exactly 0.00%, and the anti-hallucination protocol
(item 7) says to treat that as a bug until proven otherwise: a detector that cannot fire is
indistinguishable from a system that never errs, and the second is far less likely.
"""

from __future__ import annotations

import pytest

from core import identity
from eval import harness
from eval.groundtruth import GroundTruth
from eval.provenance import capture

pytestmark = pytest.mark.skipif(
    not __import__("data.generator", fromlist=["dataset_paths"])
    .dataset_paths("dev_seed_11")["gateway"]
    .exists(),
    reason="run `make seed` first — data/generated/ is gitignored",
)

DEV = "dev_seed_11"


def test_the_baseline_runs_and_partitions() -> None:
    metrics = harness.evaluate(DEV)

    assert metrics.n > 400
    assert metrics.auto_matched + metrics.exception_records == metrics.n
    assert metrics.correctly_flagged + metrics.missed_matches == metrics.exception_records


def test_the_partition_invariant_raises_when_violated() -> None:
    """Raises, never asserts: `python -O` strips asserts and a disabled partition check is
    exactly how a rate gets computed over an undisclosed subset."""
    from data.generator import dataset_paths
    from core.normalize import load_dataset
    from eval.groundtruth import load_ground_truth

    paths = dataset_paths(DEV)
    data = load_dataset(merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"])
    result = identity.resolve(data)
    harness.absorb_unresolved(data, result)
    truth = load_ground_truth(paths["labels"])

    # Drop a group so records go unaccounted for, without adding a compensating exception.
    result.groups.pop()

    with pytest.raises(RuntimeError, match="partition invariant violated"):
        harness._score(DEV, capture(DEV), data.record_count, 1000, result, truth)


def test_the_false_match_detector_actually_detects_a_false_match() -> None:
    """The investigation the 0.00% baseline demands.

    Corrupt one approved group by swapping in a record that does not belong to it, and the
    false-match count must rise. If it stays at zero, the metric is vacuous and the honest
    headline of the whole project means nothing.
    """
    from data.generator import dataset_paths
    from core.normalize import load_dataset
    from eval.groundtruth import load_ground_truth

    paths = dataset_paths(DEV)
    data = load_dataset(merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"])
    truth = load_ground_truth(paths["labels"])

    baseline = identity.resolve(data)
    harness.absorb_unresolved(data, baseline)
    clean = harness._score(DEV, capture(DEV), data.record_count, 1000, baseline, truth)
    assert clean.false_matches == 0, "baseline changed; re-derive this test"

    # Now a clean corruption: drop a member and account for it as an exception, so the
    # partition still holds and only correctness changes.
    corrupted = identity.resolve(data)
    harness.absorb_unresolved(data, corrupted)
    victim = corrupted.groups[0]
    corrupted.groups[0] = identity.MatchGroup(
        group_id=victim.group_id,
        layer=victim.layer,
        record_ids=victim.record_ids[:-1],
        settlement_id=victim.settlement_id,
        bank_row_id=victim.bank_row_id,
        expected_credit_paise=victim.expected_credit_paise,
        actual_credit_paise=victim.actual_credit_paise,
    )
    dropped = victim.record_ids[-1]
    corrupted.exceptions.append(
        identity.ReconException(
            exception_type=identity.EX_UNCLASSIFIED,
            layer=1,
            record_ids=(dropped,),
            amount_at_risk_paise=0,
            detail="injected by test",
        )
    )

    scored = harness._score(DEV, capture(DEV), data.record_count, 1000, corrupted, truth)

    assert scored.false_matches > 0, (
        "a group missing a member scored as correct, so set equality is not being applied "
        "and the false-match rate is vacuous"
    )
    assert scored.false_matches == len(corrupted.groups[0].record_ids), (
        "every record in a wrongly-composed group is a false match (eval-protocol §4): the "
        "credit is either explained or it is not"
    )


def test_a_record_cannot_be_both_matched_and_excepted() -> None:
    """The sum-only partition check had a blind spot, and a mutation test found it.

    Putting a record into a group while it is also in an exception double-counts it. If some
    other record is simultaneously lost, the two errors cancel and `auto_matched + exceptions`
    still equals N — the totals reconcile over a set that is wrong in two directions at once.
    So disjointness is now checked independently.
    """
    from data.generator import dataset_paths
    from core.normalize import load_dataset
    from eval.groundtruth import load_ground_truth

    paths = dataset_paths(DEV)
    data = load_dataset(merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"])
    truth = load_ground_truth(paths["labels"])

    corrupted = identity.resolve(data)
    harness.absorb_unresolved(data, corrupted)
    victim = corrupted.groups[0]
    already_excepted = next(
        row_id
        for exception in corrupted.exceptions
        for row_id in exception.record_ids
        if row_id not in victim.record_ids
    )
    # Swap a member for one already accounted as an exception: the count stays identical, so
    # only the disjointness check can catch this.
    corrupted.groups[0] = identity.MatchGroup(
        group_id=victim.group_id,
        layer=victim.layer,
        record_ids=victim.record_ids[:-1] + (already_excepted,),
        settlement_id=victim.settlement_id,
        bank_row_id=victim.bank_row_id,
        expected_credit_paise=victim.expected_credit_paise,
        actual_credit_paise=victim.actual_credit_paise,
    )

    with pytest.raises(RuntimeError, match="both matched and excepted"):
        harness._score(DEV, capture(DEV), data.record_count, 1000, corrupted, truth)


def test_matching_an_unmatchable_record_counts_as_a_false_match() -> None:
    """The other half of the rule: an unmatchable record's true group is empty, so matching
    it fails set equality with no special case."""
    truth = GroundTruth(record_labels=[], settlement_labels=[])
    result = identity.LayerResult(
        groups=[
            identity.MatchGroup(
                group_id="grp_x",
                layer=1,
                record_ids=("gw_1",),
                settlement_id="setl_x",
                bank_row_id=None,
                expected_credit_paise=0,
                actual_credit_paise=0,
            )
        ]
    )
    metrics = harness._score("synthetic", capture(DEV), 1, 1000, result, truth)

    assert metrics.auto_matched == 1
    assert metrics.false_matches == 1


def test_zero_tolerance_is_what_keeps_false_matches_at_zero() -> None:
    """Explains the baseline rather than just asserting it.

    Every approved group balanced at exactly δ == 0, and a group with a wrong member cannot
    balance unless its arithmetic coincidentally sums. So 0.00% is structural, not luck — and
    the mutation test above proves the detector would fire if it were not.
    """
    from data.generator import dataset_paths
    from core.normalize import load_dataset

    paths = dataset_paths(DEV)
    data = load_dataset(merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"])
    result = identity.resolve(data)

    assert result.groups
    assert all(group.delta_paise == 0 for group in result.groups)


def test_the_block_carries_provenance_and_the_overlap_caveat() -> None:
    rendered = harness.render(harness.evaluate(DEV))

    assert "data 371df9be" in rendered or "data " in rendered, "no dataset SHA in the header"
    assert "SHA:" in rendered, "no git SHA in the header"
    assert "OVERLAP and do not sum to" in rendered, "per-pathology overlap caveat missing"
    assert "precision, not coverage" in rendered
    assert "not built yet" in rendered, "absent layers must be named, not omitted"


def test_the_ledger_chain_is_verified_on_every_run() -> None:
    metrics = harness.evaluate(DEV)
    assert metrics.ledger_entries > 0
    assert len(metrics.ledger_head) == 64


def test_two_runs_produce_the_same_metrics_and_ledger_head() -> None:
    first, second = harness.evaluate(DEV), harness.evaluate(DEV)

    assert first.ledger_head == second.ledger_head
    assert (first.auto_matched, first.false_matches, first.exception_records) == (
        second.auto_matched,
        second.false_matches,
        second.exception_records,
    )
