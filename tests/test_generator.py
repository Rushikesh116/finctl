"""Tests for the scenario config and the generated datasets.

Two halves, with different lifetimes:

* **Config tests run now.** `data/scenarios.toml` exists, so its shape is checkable
  immediately — a typo'd weight or a missing pathology fails here rather than surfacing as a
  strange dataset three phases later.
* **Dataset tests skip until `data/generator.py` lands.** Clearing those skips is a Phase 1
  gate criterion (`docs/PROGRESS.md`).

The δ ≠ 0 test deliberately recomputes the trivial join and the settlement identity **from
the emitted records**, rather than trusting anything the generator reports about itself. A
generator that could self-certify its own difficulty target would be certifying nothing.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = REPO_ROOT / "data" / "scenarios.toml"
GENERATOR_MODULE = REPO_ROOT / "data" / "generator.py"

DATASETS = ["dev_seed_11", "holdout_seed_97"]
PATHOLOGY_COUNT = 12

# Mechanisms that must be present, and the ones with a hard per-dataset floor. See
# docs/SPEC.md §4.1.
REQUIRED_MECHANISMS = frozenset(
    {
        "export_cutoff_skew",
        "on_hold_release_misdated",
        "credit_without_parseable_utr",
        "duplicate_reference_contamination",
        "multiple_subsets_explain_delta",
        "pool_beyond_node_budget",
    }
)


def load_config() -> dict:
    with SCENARIOS.open("rb") as handle:
        return tomllib.load(handle)


# =========================================================================================
# Config — these run now
# =========================================================================================


def test_scenarios_toml_parses_with_the_stdlib() -> None:
    """D-0010: TOML via stdlib `tomllib`, so no YAML dependency was added."""
    config = load_config()
    assert {"dataset", "settlement", "mechanism", "pathology"} <= set(config)


def test_all_twelve_pathologies_are_configured_with_a_floor_of_two() -> None:
    """SPEC §5: all twelve must appear at least twice in *both* datasets."""
    pathologies = load_config()["pathology"]

    assert len(pathologies) == PATHOLOGY_COUNT, (
        f"expected {PATHOLOGY_COUNT} pathologies, found {sorted(pathologies)}"
    )
    assert sorted(int(k) for k in pathologies) == list(range(1, PATHOLOGY_COUNT + 1))

    for key, spec in sorted(pathologies.items()):
        assert spec.get("name"), f"pathology {key} has no name"
        assert spec.get("min_instances", 0) >= 2, (
            f"pathology {key} ({spec.get('name')}) has min_instances "
            f"{spec.get('min_instances')}; SPEC §5 requires at least 2"
        )
        assert spec.get("weight", 0) > 0, f"pathology {key} has a non-positive weight"


def test_every_delta_mechanism_is_configured() -> None:
    """If a mechanism goes missing, δ quietly trends to zero and Layer 2 becomes dead code."""
    mechanisms = load_config()["mechanism"]

    missing = REQUIRED_MECHANISMS - set(mechanisms)
    assert not missing, f"missing δ mechanisms: {sorted(missing)} (see SPEC §4.1)"

    for name, spec in sorted(mechanisms.items()):
        assert spec.get("weight", 0) > 0, f"mechanism {name} has a non-positive weight"


def test_mechanism_weights_are_a_distribution() -> None:
    weights = [spec["weight"] for spec in load_config()["mechanism"].values()]
    total = sum(weights)
    assert abs(total - 1.0) < 1e-9, f"mechanism weights sum to {total}, expected 1.0"


def test_refusal_and_overflow_mechanisms_have_a_hard_per_dataset_floor() -> None:
    """Every run must show Layer 2 succeeding, refusing, *and* giving up honestly.

    A bounded search that only ever succeeds has not demonstrated its bound; one that only
    ever times out has not demonstrated its search.
    """
    mechanisms = load_config()["mechanism"]

    assert mechanisms["multiple_subsets_explain_delta"]["min_batches_per_dataset"] >= 2
    assert mechanisms["pool_beyond_node_budget"]["min_batches_per_dataset"] >= 1


def test_the_design_target_is_stated_and_aimed_above_its_floor() -> None:
    """SPEC §4.1: aim above the floor so the assertion is not marginal."""
    settlement = load_config()["settlement"]

    floor = settlement["delta_nonzero_fraction_min"]
    target = settlement["delta_nonzero_fraction_target"]

    assert floor >= 0.30, f"the design target floor is {floor}; SPEC §4.1 states 0.30"
    assert target > floor, (
        f"generator aims at {target} against a floor of {floor}; aim higher or the test "
        "flakes the moment a weight shifts"
    )


def test_pool_beyond_node_budget_is_actually_beyond_a_plausible_budget() -> None:
    """The oversized pool has to be large enough that no reasonable budget solves it."""
    oversized = load_config()["mechanism"]["pool_beyond_node_budget"]
    assert oversized["pool_rows_min"] >= 40, (
        f"pool_rows_min is {oversized['pool_rows_min']}; below ~40 rows a competent "
        "meet-in-the-middle search solves it and SUBSET_SEARCH_EXHAUSTED never appears"
    )


def test_pathology_6_fee_bases_actually_produce_a_rounding_divergence() -> None:
    """Pathology 6 is pointless unless its fee bases make half-up non-distributive.

    Guards the config, not the arithmetic: if someone 'tidies' these to round numbers the
    GST summation rule stops being exercised by the data, and nothing else would notice.
    """
    candidates = load_config()["pathology"]["6"]["fee_base_paise_candidates"]

    def gst(base: int) -> int:
        return (base * 18 + 50) // 100

    divergent = [
        (a, b)
        for i, a in enumerate(candidates)
        for b in candidates[i:]
        if gst(a) + gst(b) != gst(a + b)
    ]
    assert divergent, (
        f"no pair in {candidates} makes summed GST differ from recomputed GST; pick fee "
        "bases whose 18% lands on a half-paisa (see money-invariants §3)"
    )


# =========================================================================================
# Generated datasets — these skip until data/generator.py lands
# =========================================================================================

generated = pytest.mark.skipif(
    not GENERATOR_MODULE.exists() or GENERATOR_MODULE.read_text(encoding="utf-8").count("\n") < 40,
    reason="data/generator.py lands in Phase 1 — see docs/PROGRESS.md",
)


def _expected_credit_paise(rows: list) -> int:
    """The recon-row form of the settlement identity (SPEC §4).

    Σ credit − Σ debit − Σ fee_base − Σ gst, in integer paise.

    `Σ gst` is a **sum of stored per-row values**, never a recomputation from the summed fee
    base: half-up rounding does not distribute over addition.
    """
    return (
        sum(r.credit_paise for r in rows)
        - sum(r.debit_paise for r in rows)
        - sum(r.fee_base_paise for r in rows)
        - sum(r.gst_paise for r in rows)
    )


def _deltas_under_the_trivial_join(dataset) -> dict[str, int]:
    """δ per bank credit, computed the way a naive Layer 1 would.

    Joins gateway rows to bank credits on `settlement_utr` == `reference` and nothing else —
    no date partitioning, no narration parsing, no handling of the unassigned pool. This is
    the baseline the design target is defined against, so it is recomputed here rather than
    read from the generator.
    """
    by_utr: dict[str, list] = defaultdict(list)
    for row in dataset.gateway_rows:
        if row.settlement_utr:
            by_utr[row.settlement_utr].append(row)

    deltas: dict[str, int] = {}
    for bank_row in dataset.bank_rows:
        if bank_row.credit_paise <= 0:
            continue  # debits are not settlement credits
        joined = by_utr.get(bank_row.reference, [])
        deltas[bank_row.row_id] = bank_row.credit_paise - _expected_credit_paise(joined)
    return deltas


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_delta_nonzero_fraction_meets_design_target(dataset_name: str) -> None:
    """SPEC §4.1: at least 30% of batches must have δ ≠ 0 under the trivial join.

    This is the test that keeps Layer 2 from being dead code. If it fails low, the generator
    has made reconciliation a join and the bounded subset search will never fire — which
    would make Phase 3 unmeasurable and the ablation table show it bought nothing.
    """
    from data.generator import generate_dataset

    config = load_config()
    floor = config["settlement"]["delta_nonzero_fraction_min"]

    dataset = generate_dataset(dataset_name)
    deltas = _deltas_under_the_trivial_join(dataset)

    assert deltas, "no settlement credits found in the bank statement"

    nonzero = {rid: d for rid, d in deltas.items() if d != 0}
    fraction = len(nonzero) / len(deltas)

    assert fraction >= floor, (
        f"{dataset_name}: only {len(nonzero)}/{len(deltas)} batches "
        f"({fraction:.1%}) have δ != 0, below the {floor:.0%} design target. "
        "The trivial settlement_utr join is resolving too much, so Layer 2 has no job. "
        "See docs/SPEC.md §4.1."
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_delta_is_signed_in_both_directions(dataset_name: str) -> None:
    """δ > 0 means members are in the unassigned pool; δ < 0 means the join over-collected.

    Both must occur, or one whole class of mechanism is absent and the search only ever has
    to add rows, never to reject them.
    """
    from data.generator import generate_dataset

    deltas = list(_deltas_under_the_trivial_join(generate_dataset(dataset_name)).values())

    assert any(d > 0 for d in deltas), "no batch is short rows (M1/M2 absent)"
    assert any(d < 0 for d in deltas), "no batch over-collected rows (M4 absent)"


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_unassigned_pool_is_the_search_space_and_is_bounded(dataset_name: str) -> None:
    """The pool is what gets searched, so its size is what makes the search tractable."""
    from data.generator import generate_dataset

    config = load_config()["settlement"]
    dataset = generate_dataset(dataset_name)

    pool = [r for r in dataset.gateway_rows if not r.settlement_id]
    share = len(pool) / len(dataset.gateway_rows)

    assert pool, "the unassigned pool is empty, so δ can only ever be 0"
    assert 0.05 <= share <= 0.25, (
        f"{dataset_name}: unassigned pool is {share:.1%} of gateway rows; SPEC §4.1 targets "
        f"~{config['unassigned_pool_share']:.0%}. Too small and Layer 2 starves; too large "
        "and the dataset stops resembling a real export."
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_both_datasets_contain_every_pathology_at_least_twice(dataset_name: str) -> None:
    """SPEC §5, asserted per dataset rather than across their union."""
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    counts: dict[int, int] = defaultdict(int)
    for label in dataset.labels:
        counts[label.pathology] += 1

    thin = {p: counts.get(p, 0) for p in range(1, PATHOLOGY_COUNT + 1) if counts.get(p, 0) < 2}
    assert not thin, f"{dataset_name}: pathologies appearing fewer than twice: {thin}"


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_generation_is_deterministic(dataset_name: str) -> None:
    """Same seed, same dataset — the precondition for a replayable run (invariant 4)."""
    from data.generator import generate_dataset

    first, second = generate_dataset(dataset_name), generate_dataset(dataset_name)

    assert [r.row_id for r in first.gateway_rows] == [r.row_id for r in second.gateway_rows]
    assert _deltas_under_the_trivial_join(first) == _deltas_under_the_trivial_join(second)


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_both_forms_of_the_settlement_identity_agree(dataset_name: str) -> None:
    """SPEC §4: the semantic form and the recon-row form must give the identical integer.

    If they disagree, one of the two is a misreading of the domain and every metric built on
    top of it is worthless.
    """
    from data.generator import generate_dataset, semantic_expected_credit_paise

    dataset = generate_dataset(dataset_name)
    by_settlement: dict[str, list] = defaultdict(list)
    for row in dataset.gateway_rows:
        if row.settlement_id:
            by_settlement[row.settlement_id].append(row)

    assert by_settlement, "no settled rows to check the identity against"

    for settlement_id, rows in sorted(by_settlement.items()):
        recon_form = _expected_credit_paise(rows)
        semantic_form = semantic_expected_credit_paise(rows)
        assert recon_form == semantic_form, (
            f"{dataset_name} {settlement_id}: recon-row form {recon_form} != semantic form "
            f"{semantic_form}. One of the two misreads the domain (SPEC §4)."
        )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_dispute_legs_are_distinguishable_from_unexplained_adjustments(
    dataset_name: str,
) -> None:
    """SPEC §5.1: `dispute_id` is the only field separating pathology 5 from pathology 11."""
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    adjustments = [r for r in dataset.gateway_rows if r.type == "adjustment"]
    assert adjustments, "no adjustment rows generated"

    dispute_legs = [r for r in adjustments if r.dispute_id]
    unexplained = [
        r for r in adjustments if not r.dispute_id and not r.order_id and not r.payment_id
    ]

    assert len(dispute_legs) >= 4, "pathology 5 needs both legs, twice over"
    assert len(unexplained) >= 2, "pathology 11 needs at least two instances"

    # Both legs of each dispute must be present, one debit and one credit.
    legs_by_dispute: dict[str, list] = defaultdict(list)
    for row in dispute_legs:
        legs_by_dispute[row.dispute_id].append(row)

    paired = [d for d, legs in legs_by_dispute.items() if len(legs) == 2]
    assert paired, "no dispute has both a chargeback and a representment leg"
    for dispute_id in paired:
        legs = legs_by_dispute[dispute_id]
        assert {bool(legs[0].debit_paise), bool(legs[1].debit_paise)} == {True, False}, (
            f"dispute {dispute_id} legs are not one debit and one credit (SPEC §5.1)"
        )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_absent_counterparts_have_no_group(dataset_name: str) -> None:
    """SPEC §3.8: pathology 8 rows are `unmatchable` with a reason, not members of a group.

    That is what makes raising MISSING_BANK_ROW score as *correctly flagged* rather than as a
    missed match against a group no bank row could complete.
    """
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    pathology_8 = [label for label in dataset.labels if label.pathology == 8]
    assert pathology_8, "pathology 8 absent"

    for label in pathology_8:
        assert label.unmatchable, f"{label.row_id}: pathology 8 row should be unmatchable"
        assert label.true_group_id is None, f"{label.row_id}: unmatchable row has a group"
        assert label.reason_code, f"{label.row_id}: unmatchable row needs a reason_code"


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_every_record_has_exactly_one_label(dataset_name: str) -> None:
    """SPEC §3.7: one label per record, no third state, no record labelled twice."""
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    record_ids = {r.row_id for r in dataset.merchant_rows}
    record_ids |= {r.row_id for r in dataset.gateway_rows}
    record_ids |= {r.row_id for r in dataset.bank_rows}

    label_ids = [label.row_id for label in dataset.labels]

    assert len(label_ids) == len(set(label_ids)), "a record is labelled more than once"
    assert set(label_ids) == record_ids, (
        f"labels and records disagree: {len(record_ids - set(label_ids))} unlabelled, "
        f"{len(set(label_ids) - record_ids)} labels with no record"
    )
