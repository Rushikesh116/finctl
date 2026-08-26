"""Evaluation harness — the measurement is the product.

Built ahead of three of the four matching layers, on purpose: every later improvement is then
measured against a *recorded* baseline rather than a remembered one. Metric definitions,
denominators and the block format are specified in `.claude/skills/eval-protocol/SKILL.md` and
frozen with `docs/SPEC.md`.

Whatever this prints is what ships. Nothing here is tuned to hit a number.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from audit.ledger import AuditLedger, verify_chain
from core import adjudicate, assignment, identity, llm, results, settlement
from core.rules_cache import RulesCache
from core.money import format_rupees
from core.results import EX_AMBIGUOUS
from core.normalize import NormalizedDataset, load_dataset
from data.generator import DATASET_SEEDS, dataset_paths
from eval.groundtruth import GroundTruth, load_ground_truth
from eval.provenance import RunProvenance, capture

PHASE = 5

# Layers that exist. Printed alongside the ones that do not, so the block never implies
# coverage from a layer that has not been written.
BUILT_LAYERS = {1: "exact", 2: "netting", 3: "fuzzy", 4: "LLM+verified"}
PLANNED_LAYERS: dict[int, str] = {}

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

# The two kinds of principled refusal, reported as separate lines forever.
#
# They were conflated once — Phase 1 mapped mechanism M5 to pathology 7 because SPEC §4.1
# describes them as sharing a *principle*, and the result was that `P7 46/46` was dominated by
# 14 perfectly matchable M5 batch rows and reported on the wrong population. Separating them
# permanently is cheaper than remembering not to re-conflate them.
#
# They are genuinely different questions:
#   record-level tie  — WHICH of two identical candidates is this record's counterparty?
#                       (pathology 7: same amount, same day, no distinguishing key)
#   subset ambiguity  — WHICH subset of pool rows settled in this batch?
#                       (mechanism M5: several subsets close δ equally well)
# One is about attribution between records, the other about set membership in a batch. A single
# "refusals" number cannot tell you which of the two a system is bad at.
REFUSAL_RECORD_TIE = "P7 record-level tie"
REFUSAL_SUBSET = "M5 batch subset ambiguity"


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
    llm_cache_hits: int = 0
    llm_calls_by_kind: dict[str, int] = field(default_factory=dict)
    llm_mode: str = "-"
    llm_stubbed: bool = False
    cost_micros_usd: int = 0
    rules_total: int = 0
    rules_promoted: int = 0
    adjudication: adjudicate.AdjudicationReport | None = None
    by_mechanism: dict[str, dict[str, int]] = field(default_factory=dict)
    unclassified_records: int = 0
    # Refusal kinds, reported separately and permanently. See REFUSAL_KINDS.
    refusals: dict[str, tuple[int, int]] = field(default_factory=dict)
    ledger_entries: int = 0
    ledger_head: str = ""

    def pct(self, numerator: int, denominator: int) -> str:
        return f"{100 * numerator / denominator:.1f}%" if denominator else "n/a"


def _percent(numerator: int, denominator: int, places: int = 1) -> str:
    if not denominator:
        return "n/a"
    return f"{100 * numerator / denominator:.{places}f}%"


def absorb_unresolved(data: NormalizedDataset, result: identity.LayerResult) -> int:
    """Terminal classification: type everything that survived the whole cascade. Returns how many.

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

    # An unassigned gateway row that no batch needed is a pending writeback: its settlement
    # falls outside this export period. That is a real, actionable verdict, and separating it
    # from UNCLASSIFIED is the difference between a queue an operator can work and one that
    # says "unclassified" fifty times. It is classified HERE, after every layer has had its
    # chance at those rows, rather than inside Layer 2.
    pool = set(result.pool_row_ids)
    nets = {row.row_id: row.net_paise for row in data.gateway_rows}
    pending = [row_id for row_id in leftover if row_id in pool]
    unknown = [row_id for row_id in leftover if row_id not in pool]

    if pending:
        result.exceptions.append(
            results.ReconException(
                exception_type=results.EX_TIMING_OUTSIDE_WINDOW,
                layer=max(BUILT_LAYERS),
                record_ids=tuple(pending),
                amount_at_risk_paise=sum(abs(nets.get(r, 0)) for r in pending),
                detail=(
                    f"{len(pending)} gateway rows carry no settlement assignment, and no "
                    "in-period batch needed them. Their settlement falls outside the period "
                    "this export covers - pending writeback, not a reconciliation failure."
                ),
            )
        )
    if unknown:
        result.exceptions.append(
            results.ReconException(
                exception_type=results.EX_UNCLASSIFIED,
                layer=max(BUILT_LAYERS),
                record_ids=tuple(unknown),
                amount_at_risk_paise=0,
                detail=(
                    f"{len(unknown)} records reached no layer that could classify them. "
                    "Every record in this bucket has a home in the enum, so the count measures "
                    "layers not yet built rather than records that defy classification."
                ),
            )
        )
    return len(leftover)


def _unlinked_batches(
    data: NormalizedDataset, result: identity.LayerResult
) -> list[results.CandidateBatch]:
    """Settlements Layer 1 raised MISSING_BANK_ROW for, rebuilt as Layer 4 input.

    Reconstructed from the dataset rather than threaded through the cascade, so Layer 4 depends
    on the *data* rather than on an earlier layer's bookkeeping.
    """
    named = {
        row_id
        for e in result.exceptions
        if e.exception_type == results.EX_MISSING_BANK_ROW
        for row_id in e.record_ids
    }
    if not named:
        return []

    batches: dict[str, list] = {}
    for row in data.gateway_rows:
        if row.settlement_id and row.row_id in named:
            batches.setdefault(row.settlement_id, []).append(row)

    merchant_by_receipt: dict[str, list[str]] = {}
    for row in data.merchant_rows:
        merchant_by_receipt.setdefault(row.order_ref, []).append(row.row_id)

    out = []
    for settlement_id in sorted(batches):
        members = sorted(batches[settlement_id], key=lambda r: r.row_id)
        utr = next((m.settlement_utr for m in members if m.settlement_utr), None)
        merchant_ids = tuple(
            sorted(
                mid
                for m in members
                if m.order_receipt
                for mid in merchant_by_receipt.get(m.order_receipt, [])
            )
        )
        out.append(
            results.CandidateBatch(
                settlement_id=settlement_id,
                settlement_utr=utr,
                bank_row_id="",
                delta_paise=0,
                member_row_ids=tuple(m.row_id for m in members),
                merchant_row_ids=merchant_ids,
                settled_at_utc=next((m.settled_at_utc for m in members if m.settled_at_utc), None),
            )
        )
    return out


def _withdraw_superseded(
    exceptions: list[results.ReconException], groups: list[results.MatchGroup]
) -> list[results.ReconException]:
    """Remove matched records from exceptions, dropping any exception left empty.

    Keeping both would put a record in a group and an exception at once, which the partition
    invariant forbids for a good reason: counted twice, it can mask an equal number of records
    lost elsewhere and the totals still reconcile.
    """
    matched = {row_id for group in groups for row_id in group.record_ids}
    if not matched:
        return exceptions

    kept: list[results.ReconException] = []
    for exception in exceptions:
        remaining = tuple(r for r in exception.record_ids if r not in matched)
        if not remaining:
            continue
        if len(remaining) == len(exception.record_ids):
            kept.append(exception)
            continue
        kept.append(
            dataclasses.replace(
                exception,
                record_ids=remaining,
                detail=exception.detail
                + f" [narrowed: {len(exception.record_ids) - len(remaining)} of its records "
                "were resolved by a later layer]",
            )
        )
    return kept


def evaluate(
    dataset_name: str, *, db_path: Path | None = None, max_layer: int = max(BUILT_LAYERS)
) -> Metrics:
    """Run the cascade over one dataset and measure it against ground truth.

    `max_layer` stops the cascade early, which is how the ablation table is produced: each arm
    is a real run on the *same* data, not a remembered number from an earlier phase. That
    matters because the datasets themselves change between phases (D-0020 moved M6's hardness
    knob and regenerated both), so a cross-phase comparison of headline rates would be
    comparing different inputs — which is precisely what the dataset SHA column exists to make
    visible.
    """
    paths = dataset_paths(dataset_name)
    provenance = capture(dataset_name)

    started = time.perf_counter()
    data = load_dataset(
        merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"]
    )
    result = identity.resolve(data)

    # Layer 2 receives exactly what Layer 1 could not settle — batches that joined but did not
    # balance, plus the unassigned pool it may draw on. Nothing else crosses the boundary.
    if max_layer >= settlement.LAYER:
        result.merge(
            settlement.resolve(
                data,
                result.candidates,
                result.pool_row_ids,
                node_budget=int(
                    os.environ.get("FINCTL_SUBSET_NODE_BUDGET", settlement.DEFAULT_NODE_BUDGET)
                ),
                timeout_ms=int(
                    os.environ.get("FINCTL_SUBSET_TIMEOUT_MS", settlement.DEFAULT_TIMEOUT_MS)
                ),
                max_evidence=int(
                    os.environ.get(
                        "FINCTL_AMBIGUOUS_SUBSETS_RECORDED_MAX",
                        settlement.DEFAULT_MAX_EVIDENCE,
                    )
                ),
            )
        )

    # Layer 3 receives every record Layers 1-2 left unmatched. Pool rows reach it because the
    # pending-writeback sweep is a terminal classification, not Layer 2's to make.
    if max_layer >= assignment.LAYER:
        matched = {row_id for group in result.groups for row_id in group.record_ids}
        named = {row_id for e in result.exceptions for row_id in e.record_ids}
        spoken_for = matched | named
        result.merge(
            assignment.resolve(
                data,
                [r.row_id for r in data.merchant_rows if r.row_id not in spoken_for],
                [r.row_id for r in data.gateway_rows if r.row_id not in spoken_for],
                window_days=int(
                    os.environ.get("FINCTL_DATE_WINDOW_DAYS", assignment.DEFAULT_DATE_WINDOW_DAYS)
                ),
            )
        )

    proposer = None
    report = None
    if max_layer >= adjudicate.LAYER:
        rules_path = Path(os.environ.get("FINCTL_RULES_CACHE", "fixtures/rules_cache.json"))
        rules = RulesCache.load(rules_path)
        proposer = llm.build_proposer()

        # Layer 4 receives the settlements Layer 1 could not link to a credit -- the
        # MISSING_BANK_ROW population -- plus every exception raised so far, for job 3.
        unlinked = [
            c
            for c in _unlinked_batches(data, result)
        ]
        fourth, report, _explanations = adjudicate.resolve(
            data, unlinked, list(result.exceptions), proposer=proposer, rules=rules
        )
        result.merge(fourth)
        # A later layer resolving a record supersedes any earlier verdict about it. Narrow
        # rather than drop: an exception naming five records of which one is now matched still
        # has something true to say about the other four, and deleting it would lose that.
        # Generalised deliberately -- the first version withdrew only MISSING_BANK_ROW and left
        # MISSING_GATEWAY_ROW behind for the same credits, which the disjointness check caught.
        result.exceptions = _withdraw_superseded(result.exceptions, result.groups)
        rules.save(rules_path)

    absorb_unresolved(data, result)
    elapsed_us = int((time.perf_counter() - started) * 1_000_000)

    truth = load_ground_truth(paths["labels"])
    metrics = _score(dataset_name, provenance, data.record_count, elapsed_us, result, truth)

    if proposer is not None:
        metrics.llm_calls = proposer.stats.calls
        metrics.llm_cache_hits = proposer.stats.cache_hits
        metrics.llm_calls_by_kind = dict(proposer.stats.calls_by_schema)
        metrics.llm_mode = proposer.mode
        metrics.llm_stubbed = proposer.stats.is_stubbed
        metrics.cost_micros_usd = proposer.stats.cost_micros_usd
    if report is not None:
        metrics.adjudication = report
        rules_now = RulesCache.load(
            Path(os.environ.get("FINCTL_RULES_CACHE", "fixtures/rules_cache.json"))
        )
        metrics.rules_total = len(rules_now)
        metrics.rules_promoted = len(rules_now.promoted)

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
            outcome="needs_layer_3",
            confidence=0,
            detail=f"delta {candidate.delta_paise} paise against {candidate.bank_row_id}",
        )
    for exception in sorted(result.exceptions, key=lambda e: (e.exception_type, e.record_ids)):
        detail = exception.detail
        if exception.evidence_found:
            # The evidence is part of the decision, so it belongs in the hash-chained record
            # and not only in the rendered block.
            shown = "; ".join(
                f"[{'+'.join(e.row_ids)}]={e.sum_paise}" for e in exception.evidence
            )
            detail += (
                f" | evidence {len(exception.evidence)}/{exception.evidence_found}"
                f"{' (truncated)' if exception.evidence_truncated else ''}"
                f"{'' if exception.evidence_complete else ' (count is a lower bound)'}: {shown}"
            )
        ledger.record(
            layer=exception.layer,
            decision="raise_exception",
            record_ids=exception.record_ids,
            outcome=exception.exception_type,
            confidence=100,
            detail=detail,
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

    # --- refusals, reported as two distinct kinds ----------------------------------------
    # A refusal must be *declared*, not merely absent. Scoring "did not match it" as a refusal
    # gives full marks for never reaching the record at all, which is how a layer that does not
    # exist yet scores 100% on the pathology it was built to handle. So the record has to land
    # in an AMBIGUOUS exception specifically.
    typed_ambiguous = {
        row_id
        for exception in result.exceptions
        if exception.exception_type == EX_AMBIGUOUS
        for row_id in exception.record_ids
    }
    p7_records = [label for label in truth.record_labels if 7 in label.pathologies]
    p7_correct = sum(
        1
        for label in p7_records
        if label.unmatchable and label.row_id not in engine_group
        and label.row_id in typed_ambiguous
    )
    metrics.refusals[REFUSAL_RECORD_TIE] = (p7_correct, len(p7_records))

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

        # Attribute on the batch's OWN rows, excluding its pool rows. A batch-level exception
        # covers the joined members; its pool rows may land in a different exception entirely
        # (a pending row goes to TIMING_OUTSIDE_WINDOW), and letting those outvote the batch's
        # own verdict reported M6 as "pending" when it had in fact exhausted its budget.
        pool = set(settlement.pool_row_ids)
        members = [r for r in settlement.true_member_row_ids if r not in pool]
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

    m5 = metrics.by_mechanism.get("multiple_subsets_explain_delta", {})
    metrics.refusals[REFUSAL_SUBSET] = (m5.get(OUTCOME_REFUSED, 0), m5.get("batches", 0))

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
        f"LLM calls          {metrics.llm_calls:>10}   "
        f"cache hits {metrics.llm_cache_hits:>4}   "
        f"Calls / 100  {(100 * metrics.llm_calls / metrics.n) if metrics.n else 0:.2f}",
        "  by kind: "
        + (
            ", ".join(f"{k} {v}" for k, v in sorted(metrics.llm_calls_by_kind.items()))
            or "none (all replayed from fixtures)"
        )
        + (
            f"   MODE={metrics.llm_mode}"
            + ("  !! STUBBED PROPOSER, not a model" if metrics.llm_stubbed else "")
        ),
        f"Rules cache        {metrics.rules_total:>10} rules   {metrics.rules_promoted} promoted "
        f"from narration the seeded regex missed",
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

    if metrics.refusals:
        lines.append(
            "Refusals  (declining is a SUCCESS. Two distinct kinds, kept separate on purpose "
            "- they were conflated once). STRICTER than the by-pathology row below: that asks "
            "whether the engine avoided a wrong answer, this asks whether it gave the right "
            "answer for the right reason - a declared AMBIGUOUS, not merely an absence."
        )
        for kind, (correct, total) in metrics.refusals.items():
            unit = "batches" if kind == REFUSAL_SUBSET else "records"
            lines.append(
                f"  {kind:<28}{correct:>4}/{total:<4} {unit:<8} "
                f"{_percent(correct, total):>7}"
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


ABLATION_ARMS = (
    (1, "exact only (L1)"),
    (2, "+ netting (L2)"),
    (3, "+ fuzzy (L3)"),
    (4, "+ LLM (L4)"),
)


def render_ablation(dataset_name: str) -> str:
    """The ablation table. Every arm is a **real run on the same data**, not a recalled number.

    Arms are re-run rather than compared across phases because the datasets change between
    phases: D-0020 moved M6's hardness knob and regenerated both, so a Phase 2 headline is not
    comparable to a Phase 3 one. Re-running is the only way the deltas mean anything.
    """
    lines = [
        "",
        "Ablation (same dataset, layers enabled cumulatively)",
        "  arm                  auto-match   false-match   exceptions   UNCLASSIFIED",
    ]
    previous: Metrics | None = None
    for ceiling, label in ABLATION_ARMS:
        arm = evaluate(dataset_name, max_layer=ceiling)
        delta = ""
        if previous is not None:
            change = 100 * (arm.auto_matched - previous.auto_matched) / arm.n
            delta = f"   {change:+.1f}pp"
        lines.append(
            f"  {label:<20}{_percent(arm.auto_matched, arm.n):>10}   "
            f"{_percent(arm.false_matches, arm.auto_matched, 2):>11}   "
            f"{arm.exception_records:>10}   {arm.unclassified_records:>12}{delta}"
        )
        previous = arm
    for layer, name in sorted(PLANNED_LAYERS.items()):
        label = f"+ {name} (L{layer})"
        lines.append(
            f"  {label:<20}{'--':>10}   {'--':>11}   {'--':>10}   {'--':>12}   not built"
        )
    lines.append(
        "  False-match rate is reported on every arm: an arm that raises coverage while also"
    )
    lines.append("  raising false matches is a regression being sold as an improvement.")
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
        print(render_ablation(args.dev))

    if args.holdout:
        print()
        print(render(evaluate(args.holdout)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
