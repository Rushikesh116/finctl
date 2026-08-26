"""Evaluation harness — the measurement is the product.

Built ahead of three of the four matching layers, on purpose: every later improvement is then
measured against a *recorded* baseline rather than a remembered one. Metric definitions,
denominators and the block format are specified in `.claude/skills/eval-protocol/SKILL.md` and
frozen with `docs/SPEC.md`.

Whatever this prints is what ships. Nothing here is tuned to hit a number.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path

from audit.ledger import AuditLedger, verify_chain
from core import identity
from core.money import format_rupees
from core.normalize import NormalizedDataset, load_dataset
from data.generator import DATASET_SEEDS, dataset_paths
from eval.groundtruth import GroundTruth, load_ground_truth
from eval.provenance import RunProvenance, capture

PHASE = 2

# Layers that exist. Printed alongside the ones that do not, so the block never implies
# coverage from a layer that has not been written.
BUILT_LAYERS = {1: "exact"}
PLANNED_LAYERS = {2: "netting", 3: "fuzzy", 4: "LLM+verified"}

# What happened to a δ != 0 batch. Reported per mechanism, because "Layer 2 resolves M1 but
# not M2" is the diagnostic and a single netting aggregate hides it entirely.
OUTCOME_RESOLVED = "resolved"
OUTCOME_REFUSED = "refused"
OUTCOME_EXHAUSTED = "exhausted"
OUTCOME_UNCLASSIFIED = "unclassified"
OUTCOME_ORDER = (OUTCOME_RESOLVED, OUTCOME_REFUSED, OUTCOME_EXHAUSTED, OUTCOME_UNCLASSIFIED)

# An exception type maps to exactly one outcome. `AMBIGUOUS` is a *success* — declining when
# the data does not determine the answer — while `SUBSET_SEARCH_EXHAUSTED` is an honest
# failure. Conflating them would let a worse search improve the headline (D-0014).
EXCEPTION_OUTCOME = {
    "AMBIGUOUS": OUTCOME_REFUSED,
    "SUBSET_SEARCH_EXHAUSTED": OUTCOME_EXHAUSTED,
    "UNCLASSIFIED": OUTCOME_UNCLASSIFIED,
}

# UNCLASSIFIED is an escape hatch, and eval-protocol §6 says a non-zero count is a finding.
# Every record currently in it has a home in the enum, so the end state is zero. These are the
# per-phase ceilings that trajectory implies — see docs/PROGRESS.md.
UNCLASSIFIED_CEILING = {2: None, 3: 13, 4: 9, 5: 0, 6: 0}


@dataclass
class Metrics:
    dataset: str
    provenance: RunProvenance
    n: int = 0
    wall_clock_us: int = 0
    auto_matched: int = 0
    per_layer: dict[int, int] = field(default_factory=dict)
    false_matches: int = 0
    exception_records: int = 0
    correctly_flagged: int = 0
    missed_matches: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_class: dict[str, int] = field(default_factory=dict)
    at_risk_paise: int = 0
    per_pathology: dict[int, tuple[int, int]] = field(default_factory=dict)
    llm_calls: int = 0
    cost_micros_usd: int = 0
    by_mechanism: dict[str, dict[str, int]] = field(default_factory=dict)
    unclassified_records: int = 0
    ledger_entries: int = 0
    ledger_head: str = ""

    def pct(self, numerator: int, denominator: int) -> str:
        return f"{100 * numerator / denominator:.1f}%" if denominator else "n/a"


def _percent(numerator: int, denominator: int, places: int = 1) -> str:
    if not denominator:
        return "n/a"
    return f"{100 * numerator / denominator:.{places}f}%"


def absorb_unresolved(data: NormalizedDataset, result: identity.LayerResult) -> int:
    """Turn everything no layer settled into one explicit exception. Returns how many.

    The cascade's "nothing is silently dropped" contract, in one place. It lives here rather
    than inline in `evaluate` so that anything driving the cascade — the CLI, a test — gets
    the same behaviour; when it was inline, a test could construct a state `evaluate` never
    produces and trip the partition check on an artefact of its own setup.

    In Phase 2 this bucket is large: it is the work Layers 2-4 will do. Its count falling
    across phases is the measure of that progress, which is why it is reported rather than
    hidden.
    """
    leftover = identity.unresolved_record_ids(data, result)
    if not leftover:
        return 0

    result.exceptions.append(
        identity.ReconException(
            exception_type=identity.EX_UNCLASSIFIED,
            layer=identity.LAYER,
            record_ids=tuple(leftover),
            amount_at_risk_paise=0,
            detail=(
                f"{len(leftover)} records reached no layer that could classify them. "
                "Layers 2-4 are not built, so this count is expected to fall as they land - "
                "it is a measure of remaining work, not of a defect."
            ),
        )
    )
    return len(leftover)


def evaluate(dataset_name: str, *, db_path: Path | None = None) -> Metrics:
    """Run the cascade over one dataset and measure it against ground truth."""
    paths = dataset_paths(dataset_name)
    provenance = capture(dataset_name)

    started = time.perf_counter()
    data = load_dataset(
        merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"]
    )
    result = identity.resolve(data)

    absorb_unresolved(data, result)
    elapsed_us = int((time.perf_counter() - started) * 1_000_000)

    truth = load_ground_truth(paths["labels"])
    metrics = _score(dataset_name, provenance, data.record_count, elapsed_us, result, truth)

    ledger = _write_ledger(result, db_path)
    metrics.ledger_entries = len(ledger)
    metrics.ledger_head = ledger.head_hash

    return metrics


def _write_ledger(result: identity.LayerResult, db_path: Path | None) -> AuditLedger:
    ledger = AuditLedger()

    for group in sorted(result.groups, key=lambda g: g.group_id):
        ledger.record(
            layer=group.layer,
            decision="approve_group",
            record_ids=group.record_ids,
            outcome=group.group_id,
            confidence=100,
            detail=(
                f"identity balanced at zero tolerance: expected "
                f"{group.expected_credit_paise} == actual {group.actual_credit_paise}"
            ),
        )
    for candidate in sorted(result.candidates, key=lambda c: c.settlement_id):
        ledger.record(
            layer=identity.LAYER,
            decision="hand_on",
            record_ids=candidate.member_row_ids,
            outcome="needs_layer_2",
            confidence=0,
            detail=f"delta {candidate.delta_paise} paise against {candidate.bank_row_id}",
        )
    for exception in sorted(result.exceptions, key=lambda e: (e.exception_type, e.record_ids)):
        ledger.record(
            layer=exception.layer,
            decision="raise_exception",
            record_ids=exception.record_ids,
            outcome=exception.exception_type,
            confidence=100,
            detail=exception.detail,
        )

    verify_chain(ledger.entries)
    if db_path is not None:
        ledger.write_sqlite(db_path)
    return ledger


def _score(
    dataset_name: str,
    provenance: RunProvenance,
    record_count: int,
    elapsed_us: int,
    result: identity.LayerResult,
    truth: GroundTruth,
) -> Metrics:
    metrics = Metrics(dataset=dataset_name, provenance=provenance, n=record_count)
    metrics.wall_clock_us = elapsed_us

    labels = truth.by_row_id()
    true_groups = truth.true_groups()

    engine_group: dict[str, frozenset[str]] = {}
    for group in result.groups:
        members = frozenset(group.record_ids)
        for row_id in group.record_ids:
            engine_group[row_id] = members

    # --- matched records ---------------------------------------------------------------
    for row_id, members in sorted(engine_group.items()):
        metrics.auto_matched += 1
        layer = next(g.layer for g in result.groups if row_id in g.record_ids)
        metrics.per_layer[layer] = metrics.per_layer.get(layer, 0) + 1
        # Set equality, per eval-protocol §4. An unmatchable record's true group is empty, so
        # matching one fails equality with no special case.
        if members != true_groups.get(row_id, frozenset()):
            metrics.false_matches += 1

    # --- exception records --------------------------------------------------------------
    exception_ids: list[str] = []
    for exception in result.exceptions:
        # Counted in records, not exception objects: one UNCLASSIFIED bucket holding 193
        # records must not read as a single exception in the breakdown.
        metrics.by_type[exception.exception_type] = metrics.by_type.get(
            exception.exception_type, 0
        ) + len(exception.record_ids)
        metrics.at_risk_paise += exception.amount_at_risk_paise
        exception_ids.extend(exception.record_ids)

    seen_exceptions = sorted(set(exception_ids))
    metrics.exception_records = len(seen_exceptions)

    for row_id in seen_exceptions:
        label = labels.get(row_id)
        if label is None:
            continue
        if label.unmatchable:
            metrics.correctly_flagged += 1
            key = label.unmatchable_class or "unknown"
            metrics.by_class[key] = metrics.by_class.get(key, 0) + 1
        else:
            metrics.missed_matches += 1

    # --- the partition invariant --------------------------------------------------------
    # Raises, never asserts. `python -O` strips asserts, and a silently disabled partition
    # check is exactly how a rate gets computed over an undisclosed subset.
    #
    # Two checks, not one. The sum alone is insufficient: a record that is BOTH matched and
    # excepted is double-counted, and if some other record is simultaneously lost the two
    # errors cancel and the sum still reconciles. A mutation test found exactly that. So
    # disjointness is checked independently of the total.
    overlap = sorted(set(engine_group) & set(seen_exceptions))
    if overlap:
        raise RuntimeError(
            f"partition invariant violated: {len(overlap)} records are both matched and "
            f"excepted, e.g. {overlap[:5]}. Counting them twice can mask an equal number of "
            "lost records, leaving the totals reconciling over a set that is wrong in two "
            "directions at once."
        )

    total = metrics.auto_matched + metrics.exception_records
    if total != metrics.n:
        raise RuntimeError(
            f"partition invariant violated: auto_matched {metrics.auto_matched} + exceptions "
            f"{metrics.exception_records} = {total}, but N = {metrics.n}. "
            f"{abs(metrics.n - total)} records are unaccounted for, so every rate in this "
            "block would be computed over a subset it did not disclose."
        )

    metrics.unclassified_records = metrics.by_type.get(identity.EX_UNCLASSIFIED, 0)

    # --- per mechanism -------------------------------------------------------------------
    # Ground-truth attribution: SettlementLabel.mechanism says which δ mechanism a batch
    # exhibits, so the block can report Layer 2's behaviour per mechanism rather than as one
    # netting aggregate. `mechanism` is scoring metadata, never visible to the matcher.
    outcome_by_row: dict[str, str] = {}
    for exception in result.exceptions:
        outcome = EXCEPTION_OUTCOME.get(exception.exception_type, exception.exception_type)
        for row_id in exception.record_ids:
            # A more specific classification always beats UNCLASSIFIED.
            if outcome_by_row.get(row_id) in (None, OUTCOME_UNCLASSIFIED):
                outcome_by_row[row_id] = outcome

    for settlement in truth.settlement_labels:
        if not settlement.mechanism:
            continue
        tallies = metrics.by_mechanism.setdefault(
            settlement.mechanism, {"batches": 0, **{k: 0 for k in OUTCOME_ORDER}}
        )
        tallies["batches"] += 1

        members = list(settlement.true_member_row_ids)
        if members and all(
            row_id in engine_group
            and engine_group[row_id] == true_groups.get(row_id, frozenset())
            for row_id in members
        ):
            tallies[OUTCOME_RESOLVED] += 1
            continue

        observed = sorted({outcome_by_row.get(row_id, OUTCOME_UNCLASSIFIED) for row_id in members})
        specific = [o for o in observed if o != OUTCOME_UNCLASSIFIED]
        outcome = specific[0] if specific else OUTCOME_UNCLASSIFIED
        # An outcome outside the four canonical ones is recorded under its own exception
        # type rather than swept into an "other" bucket. A count with no name cannot be
        # acted on, and this is exactly where a surprising classification shows up.
        tallies[outcome] = tallies.get(outcome, 0) + 1

    # --- per pathology ------------------------------------------------------------------
    tally: dict[int, list[int]] = {}
    for label in truth.record_labels:
        matched = label.row_id in engine_group
        if label.unmatchable:
            correct = not matched
        else:
            correct = matched and engine_group[label.row_id] == true_groups[label.row_id]
        for pathology in label.pathologies:
            entry = tally.setdefault(pathology, [0, 0])
            entry[1] += 1
            if correct:
                entry[0] += 1
    metrics.per_pathology = {p: (v[0], v[1]) for p, v in sorted(tally.items())}

    return metrics


def _all_record_ids(truth: GroundTruth) -> list[str]:
    return [label.row_id for label in truth.record_labels]


# --- rendering ----------------------------------------------------------------------------


def render(metrics: Metrics) -> str:
    p = metrics.provenance
    seconds = metrics.wall_clock_us / 1_000_000
    throughput = int(metrics.n / seconds) if seconds > 0 else 0

    lines = [
        f"{p.header_fragment()}   SHA: {p.git_sha}   {p.started_at_utc}",
        f"Records processed  {metrics.n:>10}          "
        f"Wall clock  {seconds:>7.3f}s",
        f"Auto-matched       {metrics.auto_matched:>10}   "
        f"{_percent(metrics.auto_matched, metrics.n):>6}   Throughput  {throughput:>5} rec/s",
    ]

    for layer, name in sorted(BUILT_LAYERS.items()):
        count = metrics.per_layer.get(layer, 0)
        lines.append(
            f"  Layer {layer}  {name:<14}{count:>6}   {_percent(count, metrics.n):>6}"
        )
    for layer, name in sorted(PLANNED_LAYERS.items()):
        lines.append(f"  Layer {layer}  {name:<14}{'--':>6}   {'--':>6}   not built yet")

    lines += [
        f"False matches      {metrics.false_matches:>10}   "
        f"{_percent(metrics.false_matches, metrics.auto_matched, 2):>6}   "
        f"<- precision, not coverage",
        f"Exceptions         {metrics.exception_records:>10}   "
        f"{_percent(metrics.exception_records, metrics.n):>6}   "
        f"{format_rupees(metrics.at_risk_paise, prefix='Rs '):>18} at risk",
        f"  correctly flagged{metrics.correctly_flagged:>10}   "
        f"{_percent(metrics.correctly_flagged, metrics.exception_records):>6}",
        f"  missed matches   {metrics.missed_matches:>10}   "
        f"{_percent(metrics.missed_matches, metrics.exception_records):>6}",
        "  by type: "
        + ", ".join(f"{k} {v}" for k, v in sorted(metrics.by_type.items(), key=lambda kv: -kv[1])),
        "  by class: "
        + (", ".join(f"{k} {v}" for k, v in sorted(metrics.by_class.items())) or "none"),
        f"LLM calls          {metrics.llm_calls:>10}          "
        f"Calls / 100  {_percent(metrics.llm_calls, metrics.n, 1)}",
        f"Cost / 1000        {'Rs TBD':>10}          "
        f"USD {metrics.cost_micros_usd / 1_000_000:.6f} total",
        f"Audit ledger       {metrics.ledger_entries:>10} entries   head {metrics.ledger_head[:12]}",
    ]

    if metrics.by_mechanism:
        lines.append(
            "By mechanism  (delta != 0 batches; ground-truth attribution. refused is a "
            "SUCCESS, exhausted is an honest failure)"
        )
        for name in sorted(metrics.by_mechanism):
            t = metrics.by_mechanism[name]
            extras = sorted(k for k in t if k not in OUTCOME_ORDER and k != "batches")
            lines.append(
                f"  {name:<34}{t['batches']:>2} batch"
                f"{'es' if t['batches'] != 1 else '  '}  "
                + "  ".join(f"{key} {t[key]}" for key in OUTCOME_ORDER)
                + ("  " + "  ".join(f"{k} {t[k]}" for k in extras) if extras else "")
            )

    if metrics.unclassified_records:
        ceiling = UNCLASSIFIED_CEILING.get(PHASE)
        target = "0 by Phase 5" if ceiling is None else f"<= {ceiling} at this phase, 0 by Phase 5"
        lines.append(
            f"FINDING  UNCLASSIFIED holds {metrics.unclassified_records} records "
            f"({_percent(metrics.unclassified_records, metrics.exception_records)} of "
            f"exceptions). Target {target}."
        )
        lines.append(
            "         Every record in it has a home in the enum; the count is a measure of "
            "layers not yet built, not of records that defy classification."
        )

    lines.append(
        "By pathology  (records carry >=1, so these OVERLAP and do not sum to "
        f"{metrics.n})"
    )

    row = "  "
    for index, (pathology, (correct, total)) in enumerate(metrics.per_pathology.items(), 1):
        row += f"P{pathology:<3}{correct:>4}/{total:<4} "
        if index % 6 == 0:
            lines.append(row.rstrip())
            row = "  "
    if row.strip():
        lines.append(row.rstrip())

    return "\n".join(lines)


def render_ablation(metrics: Metrics) -> str:
    """The ablation table. One real arm in Phase 2; the rest are named, not faked."""
    lines = [
        "",
        "Ablation                    auto-match   false-match   exceptions",
        f"  deterministic (L1+L2)     {_percent(metrics.auto_matched, metrics.n):>10}   "
        f"{_percent(metrics.false_matches, metrics.auto_matched, 2):>11}   "
        f"{metrics.exception_records:>10}   <- L1 only; L2 not built",
        "  + fuzzy (L3)                      --            --           --   not built",
        "  + LLM (L4)                        --            --           --   not built",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinCtl evaluation harness")
    parser.add_argument("--dev", default="dev_seed_11")
    parser.add_argument(
        "--holdout",
        default=None,
        help=(
            "evaluate the holdout. Phase 6 only, once — iterating against it converts it "
            "into a training set and every number after that is a lie."
        ),
    )
    parser.add_argument("--ablation", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    for name in [args.dev] + ([args.holdout] if args.holdout else []):
        if name not in DATASET_SEEDS:
            parser.error(f"unknown dataset {name!r}; expected one of {sorted(DATASET_SEEDS)}")

    metrics = evaluate(args.dev, db_path=args.db)
    print(render(metrics))
    if args.ablation:
        print(render_ablation(metrics))

    if args.holdout:
        print()
        print(render(evaluate(args.holdout)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
