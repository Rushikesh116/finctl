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


def test_every_mechanism_declares_a_guaranteed_floor() -> None:
    """Weights alone let a rare mechanism vanish from a dataset entirely.

    At weight 0.03 over 30 batches, M6's expected count is ~0.9 — so a weight-only draw
    produces zero instances for a meaningful share of seeds. If the holdout is one of them,
    Phase 6's single shot reports an untested bound.
    """
    for name, spec in sorted(load_config()["mechanism"].items()):
        assert spec.get("min_instances", 0) >= 1, (
            f"mechanism {name} has no min_instances floor, so a weight-only draw could "
            "leave it absent from a dataset (see SPEC §4.1)"
        )


def test_refusal_and_overflow_mechanisms_have_the_floors_that_make_them_observable() -> None:
    """Every run must show Layer 2 succeeding, refusing, *and* giving up honestly.

    A bounded search that only ever succeeds has not demonstrated its bound; one that only
    ever times out has not demonstrated its search.
    """
    mechanisms = load_config()["mechanism"]

    assert mechanisms["multiple_subsets_explain_delta"]["min_instances"] >= 2
    assert mechanisms["pool_beyond_node_budget"]["min_instances"] >= 1
    assert mechanisms["multiple_subsets_explain_delta"][
        "min_instances_exceeding_record_cap"
    ] >= 1, "no case forces the evidence cap to truncate, so that path ships unexercised"


def test_mechanism_floors_are_consistent_with_the_batch_count_and_target() -> None:
    """The floors are the binding constraint, so `target_batches` must accommodate them.

    This test exists because the numbers genuinely collided once: 12 floors over 26 batches
    forces δ ≠ 0 on 46% of batches, contradicting a stated 40% target. The batch count was
    raised to 30 to resolve it. Without this check the two could drift apart silently and
    the generator would be chasing an impossible target.
    """
    config = load_config()
    settlement = config["settlement"]

    floor_sum = sum(spec["min_instances"] for spec in config["mechanism"].values())
    batches = settlement["target_batches"]
    forced_fraction = floor_sum / batches

    assert forced_fraction <= settlement["delta_nonzero_fraction_target"] + 1e-9, (
        f"the {floor_sum} mechanism floors force δ != 0 on {forced_fraction:.1%} of "
        f"{batches} batches, which exceeds the stated target of "
        f"{settlement['delta_nonzero_fraction_target']:.0%}. Raise target_batches or lower "
        "a floor — do not leave the generator chasing an impossible target."
    )
    assert forced_fraction >= settlement["delta_nonzero_fraction_min"], (
        f"the floors only force {forced_fraction:.1%}, below the "
        f"{settlement['delta_nonzero_fraction_min']:.0%} assertion floor, so hitting the "
        "design target would depend on a lucky weighted draw"
    )


def test_ambiguity_construction_can_actually_exceed_the_evidence_cap() -> None:
    """C(k,2) from k equal-amount rows must be able to exceed the recording cap of 5.

    Otherwise `min_instances_exceeding_record_cap` is unsatisfiable and the truncation path
    can never be exercised, however the generator is written.
    """
    from math import comb

    spec = load_config()["mechanism"]["multiple_subsets_explain_delta"]
    reachable = comb(spec["identical_amount_rows_max"], 2)

    assert reachable > 5, (
        f"{spec['identical_amount_rows_max']} equal-amount rows reach only C(k,2)="
        f"{reachable} subsets, which cannot exceed the default cap of 5"
    )


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
def test_every_mechanism_meets_its_floor_in_both_datasets(dataset_name: str) -> None:
    """Asserted per dataset, exactly as the twelve pathologies are.

    A fraction target on dev says nothing about the holdout. M6 at weight 0.03 could
    plausibly produce zero instances in a ~500-record holdout, and Phase 6 gets one shot —
    an empty SUBSET_SEARCH_EXHAUSTED column there would mean the bounded search shipped
    without ever being seen to stop.
    """
    from data.generator import generate_dataset

    mechanisms = load_config()["mechanism"]
    dataset = generate_dataset(dataset_name)

    counts: dict[str, int] = defaultdict(int)
    for label in dataset.settlement_labels:
        if label.mechanism:
            counts[label.mechanism] += 1

    below = {
        name: (counts.get(name, 0), spec["min_instances"])
        for name, spec in sorted(mechanisms.items())
        if counts.get(name, 0) < spec["min_instances"]
    }
    assert not below, (
        f"{dataset_name}: mechanisms below their guaranteed floor "
        f"(got, required): {below}. Construct the floors first, then fill the remainder by "
        "weight — a weighted draw alone cannot guarantee a floor."
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_at_least_one_ambiguity_case_exceeds_the_evidence_cap(dataset_name: str) -> None:
    """SPEC §4.2: the truncation path must be exercised, not assumed.

    Ground truth carries every δ-closing subset, so this checks the *data* can force
    truncation. Whether the engine reports it honestly is a Phase 3 test.
    """
    from data.generator import generate_dataset

    cap = 5  # FINCTL_AMBIGUOUS_SUBSETS_RECORDED_MAX default
    dataset = generate_dataset(dataset_name)

    subset_counts = [
        len(label.explaining_subsets)
        for label in dataset.settlement_labels
        if label.mechanism == "multiple_subsets_explain_delta"
    ]

    assert len(subset_counts) >= 2, f"{dataset_name}: fewer than 2 M5 cases"
    assert any(count > cap for count in subset_counts), (
        f"{dataset_name}: M5 cases have {subset_counts} closing subsets, none exceeding the "
        f"evidence cap of {cap}, so truncation never fires and that path ships unexercised"
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_every_recorded_explaining_subset_actually_closes_delta(dataset_name: str) -> None:
    """Ground truth's own subsets must each sum to δ, or the labels are wrong.

    This is the ground-truth half of SPEC §4.2's rule 1. The engine-side half — that every
    subset an AMBIGUOUS exception records also closes δ — is a Phase 3 test.
    """
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    rows_by_id = {r.row_id: r for r in dataset.gateway_rows}

    checked = 0
    for label in dataset.settlement_labels:
        for subset in label.explaining_subsets:
            total = _expected_credit_paise([rows_by_id[rid] for rid in subset])
            assert total == label.delta_paise, (
                f"{dataset_name} {label.settlement_id}: recorded subset {subset} sums to "
                f"{total}, not δ={label.delta_paise}. A subset that does not close δ is not "
                "evidence of ambiguity."
            )
            checked += 1

    assert checked, f"{dataset_name}: no explaining subsets recorded at all"


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
    """The pool is what gets searched, so its size is what makes the search tractable.

    Measured **excluding the M6 batch**, which is deliberately pathological: averaging a
    40-plus-row pool into the realism figure would let one intentional outlier decide whether
    the other 29 batches look like a real export. M6's size is asserted separately below.
    """
    from data.generator import generate_dataset

    config = load_config()["settlement"]
    dataset = generate_dataset(dataset_name)

    oversized_pool_ids = [
        row_id
        for label in dataset.settlement_labels
        if label.mechanism == "pool_beyond_node_budget"
        for row_id in label.pool_row_ids
    ]
    ordinary_rows = [r for r in dataset.gateway_rows if r.row_id not in oversized_pool_ids]
    ordinary_pool = [r for r in ordinary_rows if not r.settlement_id]
    share = len(ordinary_pool) / len(ordinary_rows)

    assert ordinary_pool, "the unassigned pool is empty, so δ can only ever be 0"
    assert 0.05 <= share <= 0.25, (
        f"{dataset_name}: ordinary unassigned pool is {share:.1%} of gateway rows; SPEC §4.1 "
        f"targets ~{config['unassigned_pool_share']:.0%}. Too small and Layer 2 starves; too "
        "large and the dataset stops resembling a real export."
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_the_oversized_pool_is_actually_oversized(dataset_name: str) -> None:
    """M6's whole job is to be too big to search within budget, so check that it is."""
    from data.generator import generate_dataset

    floor = load_config()["mechanism"]["pool_beyond_node_budget"]["pool_rows_min"]
    dataset = generate_dataset(dataset_name)

    sizes = [
        len(label.pool_row_ids)
        for label in dataset.settlement_labels
        if label.mechanism == "pool_beyond_node_budget"
    ]
    assert sizes, f"{dataset_name}: no M6 batch at all"
    assert all(size >= floor for size in sizes), (
        f"{dataset_name}: M6 pool sizes {sizes}, expected every one >= {floor}"
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_per_batch_pools_stay_in_the_tractable_band(dataset_name: str) -> None:
    """Ordinary δ batches must be solvable within budget, or Layer 2 only ever times out."""
    from data.generator import generate_dataset

    settlement = load_config()["settlement"]
    dataset = generate_dataset(dataset_name)

    oversized = [
        len(label.pool_row_ids)
        for label in dataset.settlement_labels
        if label.mechanism and label.mechanism != "pool_beyond_node_budget"
    ]
    assert oversized, f"{dataset_name}: no δ batches with a pool"
    assert max(oversized) <= settlement["pool_rows_max"], (
        f"{dataset_name}: an ordinary δ batch has a pool of {max(oversized)} rows, above the "
        f"tractable ceiling of {settlement['pool_rows_max']}"
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
def test_committed_hash_manifest_matches_a_fresh_generation() -> None:
    """D-0007: the datasets are not committed, so the freeze is a committed hash manifest.

    Trusting the seed alone is not enough — a refactor can change the *order* in which a
    refactored generator consumes the PRNG without touching the seed, silently producing a
    different dataset under identical provenance. That is the bug this catches.

    Byte-level determinism across processes and `PYTHONHASHSEED` values is verified separately
    by `make seed`; this test guards against drift over time.
    """
    from data.generator import HASH_MANIFEST, REPO_ROOT, emit, generate_dataset, sha256_of

    if not HASH_MANIFEST.exists():
        pytest.skip("run `make seed` to write data/DATASET_HASHES.txt")

    committed = HASH_MANIFEST.read_text(encoding="utf-8").strip().splitlines()

    regenerated: list[str] = []
    for name in sorted(DATASETS):
        for path in emit(generate_dataset(name)):
            regenerated.append(f"{sha256_of(path)}  {path.relative_to(REPO_ROOT)}")

    assert regenerated == committed, (
        "regenerated dataset hashes differ from the committed manifest. Either the generator "
        "changed intentionally — in which case re-run `make seed` and commit the new manifest "
        "with a DECISIONS.md note — or a refactor shifted PRNG consumption order and the "
        '"frozen" datasets quietly moved.'
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_m5_is_unmatchable_but_m6_is_not(dataset_name: str) -> None:
    """SPEC §4.3: declining and giving up must not score the same way.

    M5's δ is genuinely undetermined, so refusing is correct and its records are unmatchable.
    M6's δ *is* determined, just out of budget reach, so its records stay matchable and
    exhausting the search is an honest miss. Conflating the two would let a system score a
    timeout as a principled refusal.
    """
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    label_by_id = {label.row_id: label for label in dataset.labels}

    for settlement in dataset.settlement_labels:
        if settlement.mechanism == "multiple_subsets_explain_delta":
            for row_id in settlement.true_member_row_ids:
                assert label_by_id[row_id].unmatchable, (
                    f"{row_id}: an M5 record must be unmatchable, or refusing it scores as a "
                    "missed match and the metric punishes correct behaviour (SPEC §4.3)"
                )
        elif settlement.mechanism == "pool_beyond_node_budget":
            for row_id in settlement.true_member_row_ids:
                assert not label_by_id[row_id].unmatchable, (
                    f"{row_id}: an M6 record must stay matchable, so exhausting the budget "
                    "scores as an honest miss rather than a correct refusal (SPEC §4.3)"
                )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_unmatchable_records_split_absent_from_undetermined(dataset_name: str) -> None:
    """Both classes must occur, because the exception queue says different things about them.

    `absent` means no partner exists — chase the feed. `undetermined` means a partner exists
    but the data cannot say which — chasing the feed will not help, because the rows are
    already all there and simply do not discriminate. Collapsing the two would have the queue
    send an operator hunting for a bank row sitting right in front of them.
    """
    from core.records import REASON_CLASS
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    unmatchable = [label for label in dataset.labels if label.unmatchable]
    assert unmatchable, f"{dataset_name}: no unmatchable records at all"

    classes = defaultdict(int)
    for label in unmatchable:
        assert label.reason_code in REASON_CLASS, f"{label.reason_code} unregistered"
        classes[label.unmatchable_class] += 1

    assert classes["absent"] >= 2, f"{dataset_name}: too few `absent` records: {dict(classes)}"
    assert classes["undetermined"] >= 2, (
        f"{dataset_name}: too few `undetermined` records: {dict(classes)}"
    )


@generated
@pytest.mark.parametrize("dataset_name", DATASETS)
def test_the_two_unmatchable_classes_land_on_the_right_pathologies(dataset_name: str) -> None:
    """Pathologies 8 and 11 are `absent`; pathology 7 and M5 are `undetermined`."""
    from data.generator import generate_dataset

    dataset = generate_dataset(dataset_name)
    by_pathology: dict[int, set[str | None]] = defaultdict(set)
    for label in dataset.labels:
        if label.unmatchable:
            by_pathology[label.pathology].add(label.unmatchable_class)

    assert by_pathology[8] == {"absent"}, f"pathology 8 (feed gap): {by_pathology[8]}"
    assert by_pathology[11] == {"absent"}, f"pathology 11 (orphan adj): {by_pathology[11]}"
    assert by_pathology[7] == {"undetermined"}, f"pathology 7 (ambiguous): {by_pathology[7]}"


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
