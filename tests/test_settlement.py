"""Tests for Layer 2 — the bounded subset search.

The properties that matter are the three outcomes staying distinct, the bound actually binding,
and every recorded refusal being checkable. A search that only ever succeeds has not
demonstrated its bound; one that succeeds by picking the first of several answers has not
demonstrated anything at all.
"""

from __future__ import annotations

import pytest

from core import settlement
from core.records import GatewayRow
from core.results import EX_AMBIGUOUS, EX_SUBSET_SEARCH_EXHAUSTED

DEV, HOLDOUT = "dev_seed_11", "holdout_seed_97"


def row(row_id: str, net: int, *, created: int = 1_772_000_000) -> GatewayRow:
    """A pool row with a chosen net contribution, built through the real constructor."""
    return GatewayRow(
        row_id=row_id,
        type="payment",
        entity_id=f"pay_{row_id}",
        debit_paise=0 if net >= 0 else -net,
        credit_paise=net if net >= 0 else 0,
        fee_base_paise=0,
        gst_paise=0,
        currency="INR",
        created_at_utc=created,
    )


# --- the search ---------------------------------------------------------------------------


def test_a_unique_subset_resolves() -> None:
    outcome = settlement.search_subsets(300, [row("a", 100), row("b", 200), row("c", 750)])

    assert outcome.status == "resolved"
    assert outcome.solutions_found == 1
    assert set(outcome.solutions[0]) == {"a", "b"}
    assert outcome.complete


def test_equal_sized_alternatives_are_ambiguity_not_a_choice() -> None:
    """Two ways to make the same number *at the same size* is a refusal, not a coin flip.

    Equal size is what makes them equally plausible. Alternatives of different sizes are not
    ambiguity — minimality resolves those, and `test_the_minimal_size_wins...` covers it.
    """
    outcome = settlement.search_subsets(
        300, [row("a", 100), row("b", 200), row("c", 150), row("d", 150)]
    )

    assert outcome.status == "ambiguous"
    assert outcome.subset_size == 2, "both alternatives are size 2"
    assert outcome.solutions_found == 2


def test_the_minimal_size_wins_and_larger_sizes_are_flagged_unsearched() -> None:
    """Minimality is a stated prior, so the outcome records that it was applied."""
    # {c} sums to 300 at size 1; {a,b} also sums to 300 at size 2.
    outcome = settlement.search_subsets(300, [row("a", 100), row("b", 200), row("c", 300)])

    assert outcome.subset_size == 1
    assert outcome.larger_sizes_unsearched, (
        "the claim is minimal-explanation, not exhaustively-unique, and the audit trail must "
        "not overstate it"
    )


def test_iterative_deepening_finds_a_small_subset_among_many_candidates() -> None:
    """The regression that motivated the rewrite.

    A 3-row answer among 78 candidates is only C(78,3) ~ 76k combinations, but depth-first with
    a size cap explores enormous size-8 subtrees first and never reaches it. Deepening by size
    finds it immediately.
    """
    candidates = [row(f"r{i}", 1_000_000 + i * 7_919) for i in range(78)]
    target = sum(candidates[i].net_paise for i in (5, 40, 70))

    outcome = settlement.search_subsets(target, candidates, node_budget=200_000)

    assert outcome.status in ("resolved", "ambiguous"), (
        f"a 3-row answer among 78 candidates was not reachable: {outcome.status} after "
        f"{outcome.nodes_explored} nodes"
    )
    assert outcome.subset_size == 3
    assert outcome.nodes_explored < 200_000


def test_no_solution_is_distinguished_from_a_bounded_search() -> None:
    """"Nothing explains this" and "I ran out of budget" are different claims."""
    complete = settlement.search_subsets(7, [row("a", 100), row("b", 200)], max_subset_size=2)
    assert complete.status == "no_solution"
    assert complete.complete

    limited = settlement.search_subsets(
        7, [row(f"r{i}", 100 + i) for i in range(20)], max_subset_size=3
    )
    assert limited.status == "exhausted", "a size-limited search must not claim no_solution"
    assert limited.limit_hit == "subset_size"
    assert not limited.complete


def test_the_node_budget_binds() -> None:
    """A budget too small to finish size 2 must stop at the budget, not silently complete.

    The target has to be *plausible* for this to test anything: an absurd target is pruned by
    the size cap on the first node, so the budget would never be consulted.
    """
    candidates = [row(f"r{i}", 1_000_000 + i * 13) for i in range(60)]
    target = sum(candidates[i].net_paise for i in (3, 11, 29, 44, 57))

    outcome = settlement.search_subsets(target, candidates, node_budget=100)

    assert outcome.status == "exhausted"
    assert outcome.limit_hit == "nodes", f"stopped on {outcome.limit_hit} instead"
    assert outcome.nodes_explored <= 200, "the budget was overshot substantially"
    assert not outcome.complete


def test_negative_nets_are_handled() -> None:
    """A pool holds refunds and debit adjustments, so a one-sided prune would be unsound."""
    outcome = settlement.search_subsets(
        50, [row("a", 200), row("b", -150), row("c", 900)]
    )
    assert outcome.status == "resolved"
    assert set(outcome.solutions[0]) == {"a", "b"}


def test_solutions_are_never_double_counted() -> None:
    """The bug that manufactured false ambiguity.

    Counting mid-descent scored the same subset again at every subsequent skip node, so a
    uniquely-determined batch reported AMBIGUOUS — which scores as a *success*. Solutions are
    counted at the leaf, once per distinct include/exclude assignment.
    """
    outcome = settlement.search_subsets(
        300, [row("a", 100), row("b", 200), row("c", 999), row("d", 1_001)]
    )

    assert outcome.solutions_found == 1, (
        f"expected exactly one subset, got {outcome.solutions_found}: {outcome.solutions}"
    )


def test_evidence_is_capped_but_the_count_is_not() -> None:
    """§4.2: truncation must be visible, and `found` is the true total when complete."""
    candidates = [row(f"t{i}", 100) for i in range(7)]
    outcome = settlement.search_subsets(200, candidates, max_evidence=3)

    assert outcome.solutions_found == 21, "C(7,2) = 21"
    assert len(outcome.solutions) == 3
    assert outcome.truncated
    assert outcome.complete, "a truncated *record* is not an incomplete *search*"


def test_rows_involved_covers_solutions_the_cap_dropped() -> None:
    """An AMBIGUOUS exception must name every row the ambiguity is about.

    Recording only the capped subsets would leave the rest sitting in UNCLASSIFIED with no
    explanation attached to them.
    """
    candidates = [row(f"t{i}", 100) for i in range(7)]
    outcome = settlement.search_subsets(200, candidates, max_evidence=2)

    assert len(outcome.solutions) == 2
    assert len(outcome.rows_involved) == 7, (
        f"only {len(outcome.rows_involved)} rows named, but all 7 appear in some solution"
    )


def test_the_search_is_deterministic() -> None:
    candidates = [row(f"r{i}", 500 + (i * 37) % 91) for i in range(30)]
    first = settlement.search_subsets(1_500, candidates)
    second = settlement.search_subsets(1_500, candidates)

    assert first == second


@pytest.mark.parametrize("bad", [{"node_budget": 0}, {"timeout_ms": 0}, {"max_evidence": 0}, {"max_subset_size": 0}])
def test_nonsense_bounds_are_refused(bad: dict) -> None:
    with pytest.raises(ValueError):
        settlement.search_subsets(100, [row("a", 100)], **bad)


# --- against the real datasets -------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not __import__("data.generator", fromlist=["dataset_paths"])
    .dataset_paths(DEV)["gateway"]
    .exists(),
    reason="run `make seed` first — data/generated/ is gitignored",
)


def run_layers(name: str):
    from core import identity, normalize
    from data.generator import dataset_paths

    paths = dataset_paths(name)
    data = normalize.load_dataset(
        merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"]
    )
    first = identity.resolve(data)
    second = settlement.resolve(data, first.candidates, first.pool_row_ids)
    return data, first, second


@pytest.mark.parametrize("dataset_name", [DEV, HOLDOUT])
def test_all_three_outcomes_occur_in_both_datasets(dataset_name: str) -> None:
    """SPEC §4.1: every run must show Layer 2 succeeding, refusing, and giving up honestly.

    This asserts outcome **presence**, never a rate, so it is a structural check on whether the
    bound is exercised rather than a measurement. That distinction is what keeps it away from
    the holdout discipline: there is nothing here to tune towards.
    """
    _, _, layer2 = run_layers(dataset_name)
    types = {exception.exception_type for exception in layer2.exceptions}

    assert layer2.groups, f"{dataset_name}: Layer 2 resolved nothing, so the search is dead code"
    assert EX_AMBIGUOUS in types, (
        f"{dataset_name}: nothing refused — M5 should produce AMBIGUOUS, and a search that "
        "never declines has not demonstrated the refusal"
    )
    assert EX_SUBSET_SEARCH_EXHAUSTED in types, (
        f"{dataset_name}: nothing exhausted — M6 should hit the bound, and a bounded search "
        "that never stops has not demonstrated its bound"
    )


@pytest.mark.parametrize("dataset_name", [DEV, HOLDOUT])
def test_every_recorded_subset_closes_delta(dataset_name: str) -> None:
    """§4.2 rule 1, engine side: a subset that does not close δ is not evidence, it is a bug."""
    data, _, layer2 = run_layers(dataset_name)
    nets = {r.row_id: r.net_paise for r in data.gateway_rows}

    checked = 0
    for exception in layer2.exceptions:
        for evidence in exception.evidence:
            total = sum(nets[row_id] for row_id in evidence.row_ids)
            assert total == evidence.sum_paise, (
                f"{dataset_name}: recorded subset {evidence.row_ids} sums to {total}, not the "
                f"{evidence.sum_paise} it claims"
            )
            checked += 1
    assert checked, f"{dataset_name}: no evidence recorded on any exception"


@pytest.mark.parametrize("dataset_name", [DEV, HOLDOUT])
def test_an_exhausted_search_never_claims_a_complete_count(dataset_name: str) -> None:
    """`evidence_found` after a bounded search is a lower bound, and must say so."""
    _, _, layer2 = run_layers(dataset_name)

    exhausted = [
        e for e in layer2.exceptions if e.exception_type == EX_SUBSET_SEARCH_EXHAUSTED
    ]
    assert exhausted, f"{dataset_name}: no exhausted case to check"
    for exception in exhausted:
        assert not exception.evidence_complete, (
            "an incomplete search reported its solution count as complete"
        )


@pytest.mark.parametrize("dataset_name", [DEV, HOLDOUT])
def test_a_refusal_records_more_subsets_than_the_cap_somewhere(dataset_name: str) -> None:
    """The truncation path must be exercised, not assumed (scenarios.toml guarantees a case)."""
    _, _, layer2 = run_layers(dataset_name)

    truncated = [e for e in layer2.exceptions if e.evidence_truncated]
    assert truncated, f"{dataset_name}: no refusal truncated its evidence"
    for exception in truncated:
        assert exception.evidence_found > len(exception.evidence)


@pytest.mark.parametrize("dataset_name", [DEV, HOLDOUT])
def test_layer_2_claims_each_pool_row_at_most_once(dataset_name: str) -> None:
    """A pool row settles once. Two batches claiming it would double-count the money."""
    _, _, layer2 = run_layers(dataset_name)

    claimed = [row_id for group in layer2.groups for row_id in group.record_ids]
    assert len(claimed) == len(set(claimed)), "a record was claimed by two Layer 2 groups"


@pytest.mark.parametrize("dataset_name", [DEV, HOLDOUT])
def test_every_layer_2_group_balances_at_zero(dataset_name: str) -> None:
    """The verifier is the only approver, so every approved group must balance exactly."""
    _, _, layer2 = run_layers(dataset_name)

    assert layer2.groups
    for group in layer2.groups:
        assert group.delta_paise == 0, f"{group.group_id} approved with delta {group.delta_paise}"
