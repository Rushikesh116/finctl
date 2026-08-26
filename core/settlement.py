"""Layer 2 — settlement decomposition. The bounded subset search.

Layer 1 hands over batches that joined cleanly but did not balance, each with its δ. This layer
searches the unassigned pool for the subset of rows that explains δ.

**Three outcomes, and keeping them distinct is the point:**

| δ closed by | Outcome | Meaning |
|---|---|---|
| exactly one subset | resolved | the arithmetic determines the answer |
| two or more subsets | **refused** — `AMBIGUOUS` | the arithmetic does *not* determine it |
| search hit its bound | **exhausted** — `SUBSET_SEARCH_EXHAUSTED` | we do not know |

Refusing and exhausting are not the same claim (D-0014). Refusing is a success: the data is
genuinely undetermined and saying so is the correct answer. Exhausting is an honest failure. A
metric that merged them would reward a *worse* search — drop the node budget, time out more
often, and "refused" would climb while the engine reconciles strictly less.

**The search enumerates every solution before deciding.** Stopping at the first is wrong for
the resolve case (a second solution might exist, making it ambiguous) and stopping at the second
is wrong for the refusal case (finding 2 of 21 and refusing is right by accident). So
completeness within the bound is what licenses either conclusion, and `complete=False` blocks
both.

**The bound is the stopping rule.** A node budget, which is deterministic, plus a wall-clock
timeout, which is not — see D-0019 for why that asymmetry matters and how it is surfaced.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from core import verifier
from core.money import Paise, format_rupees
from core.normalize import NormalizedDataset
from core.records import GatewayRow
from core.results import (
    EX_AMBIGUOUS,
    EX_SUBSET_SEARCH_EXHAUSTED,
    EX_TIMING_OUTSIDE_WINDOW,
    EX_UNCLASSIFIED,
    CandidateBatch,
    GroupProposal,
    LayerResult,
    ReconException,
    SubsetEvidence,
)

__all__ = [
    "DEFAULT_MAX_EVIDENCE",
    "DEFAULT_NODE_BUDGET",
    "DEFAULT_TIMEOUT_MS",
    "LAYER",
    "SearchOutcome",
    "resolve",
    "search_subsets",
]

LAYER = 2

DEFAULT_NODE_BUDGET = 200_000
DEFAULT_TIMEOUT_MS = 2_000
DEFAULT_MAX_EVIDENCE = 5

# A settlement is short by a handful of rows, not fifty. Capping subset size turns an
# intractable search into a cheap one — but it is a **bound, not a heuristic**: if the cap
# binds and nothing is found, the outcome is `exhausted`, never `no_solution`. Reporting "no
# subset explains this" when larger subsets were simply never searched would be a false
# negative dressed as a finding.
DEFAULT_MAX_SUBSET_SIZE = 8

# Candidate windows, in days before settlement, tried in order. The documented cycle is T+2
# working days (T+5 over a holiday, pathology 9), so seven calendar days covers it with room.
#
# Widening is not a fallback bolted on for convenience — mechanism M2 exists to punish a
# naively date-windowed filter, because its rows carry `created_at` from an earlier period and
# a tidy window drops exactly the rows that explain δ. So the narrow window is tried first
# because it is cheap, and **the fact that it had to be widened is itself diagnostic**: it is
# the signature of a misdated on-hold release.
DEFAULT_WINDOW_STAGES_DAYS: tuple[int | None, ...] = (7, 14, 28, None)

# How often to consult the clock. Checking every node would make the syscall the dominant cost
# and, worse, make the node budget's effect depend on machine speed.
_CLOCK_CHECK_INTERVAL = 4_096

Status = Literal["resolved", "ambiguous", "exhausted", "no_solution"]


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What the search found, and how much it is worth trusting."""

    status: Status
    solutions: tuple[tuple[str, ...], ...]
    solutions_found: int
    truncated: bool
    complete: bool
    limit_hit: str | None
    nodes_explored: int
    # Excluded from equality: two searches that explored the same nodes and found the same
    # solutions are the same result, however long the machine took. Including wall-clock here
    # would make every outcome unequal to itself across runs and quietly defeat any
    # determinism check written against it.
    elapsed_us: int = field(compare=False)
    candidates_considered: int = 0
    subset_size: int | None = None
    larger_sizes_unsearched: bool = False
    # Every row that appeared in any solution, including ones the evidence cap did not record.
    # An AMBIGUOUS exception must name the rows the ambiguity is *about*, and recording only
    # the capped subsets would leave the rest sitting in UNCLASSIFIED, unexplained.
    rows_involved: tuple[str, ...] = ()

    @property
    def solutions_found_is_exact(self) -> bool:
        """`solutions_found` is a true total only if the search finished.

        When the bound cut it short the count is a **lower bound**, and reporting it as a total
        would overstate what is known. §4.2 requires the true count; when the search is
        incomplete, the honest report is that there is no true count yet.
        """
        return self.complete


def _search_exact_size(
    target: int,
    nets: list[int],
    row_ids: list[str],
    prefix: list[int],
    want: int,
    *,
    node_budget: int,
    deadline: float,
    max_evidence: int,
) -> tuple[list[tuple[str, ...]], int, int, str | None, tuple[str, ...]]:
    """Every subset of exactly `want` rows summing to `target`. Values sorted descending.

    Two bounds, both exact given the sort order: with `r` picks still to make from index `i`,
    the largest reachable total is the `r` largest values from `i` onward and the smallest is
    the `r` smallest values overall. A target outside that range cannot be completed.
    """
    size = len(nets)
    solutions: list[tuple[str, ...]] = []
    involved: set[str] = set()
    found = 0
    nodes = 0
    limit_hit: str | None = None
    chosen: list[str] = []

    def walk(index: int, remaining: int, picks_left: int) -> None:
        nonlocal nodes, found, limit_hit
        if limit_hit:
            return
        nodes += 1
        if nodes > node_budget:
            limit_hit = "nodes"
            return
        if nodes % _CLOCK_CHECK_INTERVAL == 0 and time.perf_counter() > deadline:
            limit_hit = "clock"
            return

        if picks_left == 0:
            if remaining == 0:
                found += 1
                involved.update(chosen)
                if len(solutions) < max_evidence:
                    solutions.append(tuple(chosen))
            return
        if size - index < picks_left:
            return

        # r largest from here, and r smallest anywhere after here.
        reachable_max = prefix[index + picks_left] - prefix[index]
        reachable_min = prefix[size] - prefix[size - picks_left]
        if remaining > reachable_max or remaining < reachable_min:
            return

        chosen.append(row_ids[index])
        walk(index + 1, remaining - nets[index], picks_left - 1)
        chosen.pop()
        if limit_hit:
            return
        walk(index + 1, remaining, picks_left)

    walk(0, target, want)
    # Sorted, not set order: the exception's record ids must be deterministic.
    return solutions, found, nodes, limit_hit, tuple(sorted(involved))


def search_subsets(
    target_paise: int,
    candidates: list[GatewayRow],
    *,
    node_budget: int = DEFAULT_NODE_BUDGET,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_evidence: int = DEFAULT_MAX_EVIDENCE,
    max_subset_size: int = DEFAULT_MAX_SUBSET_SIZE,
) -> SearchOutcome:
    """Find the subsets of `candidates` whose net sum equals `target_paise`, within a bound.

    **Iterative deepening by subset size, smallest first.** Plain depth-first with a size cap
    is the obvious implementation and it does not work: finding a 3-row subset among 78
    candidates is only C(78,3) ~ 76k combinations, but a depth-first walk explores enormous
    size-8 subtrees before it ever reaches them. The problem was never the budget, it was the
    order. Searching size 1, then 2, then 3 finds small explanations immediately.

    **The smallest size that yields any solution wins**, and larger sizes are then not
    searched. That is a *minimality prior*, not a shortcut: the smallest set of rows that
    accounts for δ is the plausible explanation, and a larger set that also happens to sum to
    δ is a coincidence — which is exactly how a human reconciler reads it. Verified against
    ground truth on every resolvable batch in both datasets: the minimal solution is always
    either uniquely the true one, or tied with equal-size alternatives that are genuinely
    ambiguous. `larger_sizes_unsearched` records that the claim is minimal-explanation rather
    than exhaustively-unique, so the audit trail does not overstate it.

    Ties at the minimal size are **ambiguity**, not a coin flip: equally-sized alternatives are
    equally plausible, so the answer is a refusal.
    """
    if node_budget <= 0:
        raise ValueError(f"node_budget must be positive, got {node_budget}")
    if timeout_ms <= 0:
        raise ValueError(f"timeout_ms must be positive, got {timeout_ms}")
    if max_evidence <= 0:
        raise ValueError(f"max_evidence must be positive, got {max_evidence}")
    if max_subset_size <= 0:
        raise ValueError(f"max_subset_size must be positive, got {max_subset_size}")

    ordered = sorted(candidates, key=lambda row: (-row.net_paise, row.row_id))
    nets = [row.net_paise for row in ordered]
    row_ids = [row.row_id for row in ordered]
    size = len(ordered)

    prefix = [0] * (size + 1)
    for index, value in enumerate(nets):
        prefix[index + 1] = prefix[index] + value

    started = time.perf_counter()
    deadline = started + timeout_ms / 1000
    total_nodes = 0
    ceiling = min(max_subset_size, size)

    for want in range(1, ceiling + 1):
        solutions, found, nodes, limit_hit, involved = _search_exact_size(
            target_paise,
            nets,
            row_ids,
            prefix,
            want,
            node_budget=node_budget - total_nodes,
            deadline=deadline,
            max_evidence=max_evidence,
        )
        total_nodes += nodes
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)

        if limit_hit:
            return SearchOutcome(
                status="exhausted",
                solutions=tuple(solutions),
                solutions_found=found,
                truncated=found > len(solutions),
                complete=False,
                limit_hit=limit_hit,
                nodes_explored=total_nodes,
                elapsed_us=elapsed_us,
                candidates_considered=size,
                subset_size=want,
                rows_involved=involved,
            )
        if found:
            return SearchOutcome(
                status="resolved" if found == 1 else "ambiguous",
                solutions=tuple(solutions),
                solutions_found=found,
                truncated=found > len(solutions),
                complete=True,
                limit_hit=None,
                nodes_explored=total_nodes,
                elapsed_us=elapsed_us,
                candidates_considered=size,
                subset_size=want,
                larger_sizes_unsearched=want < ceiling,
                rows_involved=involved,
            )

    elapsed_us = int((time.perf_counter() - started) * 1_000_000)
    # Nothing found. If the size cap was below the candidate count, larger subsets were never
    # looked at, so this is an exhausted search rather than an absence of solutions.
    size_limited = max_subset_size < size
    return SearchOutcome(
        status="exhausted" if size_limited else "no_solution",
        solutions=(),
        solutions_found=0,
        truncated=False,
        complete=not size_limited,
        limit_hit="subset_size" if size_limited else None,
        nodes_explored=total_nodes,
        elapsed_us=elapsed_us,
        candidates_considered=size,
        subset_size=ceiling,
    )


def _eligible(
    pool: list[GatewayRow], batch: CandidateBatch, window_days: int | None
) -> list[GatewayRow]:
    """Pool rows that could belong to this batch, within `window_days` before settlement.

    The hard filter is **causal** and always applies: a row cannot settle before it was
    created. The window on top of it is a domain prior — a settlement normally contains rows
    from its own cycle — and it is escalated rather than trusted, because mechanism M2 exists
    to punish a filter that trusts it. `window_days=None` means the causal constraint alone.
    """
    if batch.settled_at_utc is None:
        return list(pool)
    ceiling = batch.settled_at_utc
    floor = None if window_days is None else ceiling - window_days * 86_400
    return [
        row
        for row in pool
        if row.created_at_utc <= ceiling and (floor is None or row.created_at_utc >= floor)
    ]


def _search_staged(
    batch: CandidateBatch,
    pool: list[GatewayRow],
    claimed: set[str],
    *,
    node_budget: int,
    timeout_ms: int,
    max_evidence: int,
    max_subset_size: int,
    window_stages_days: tuple[int | None, ...],
) -> tuple[SearchOutcome, int | None]:
    """Search this batch, escalating the window until something conclusive comes back.

    Widening on *ambiguity* would be pointless: a wider window only adds candidates, so it can
    never turn ambiguity back into a unique answer.
    """
    outcome: SearchOutcome | None = None
    window_used: int | None = None
    for stage in window_stages_days:
        available = [
            row for row in _eligible(pool, batch, stage) if row.row_id not in claimed
        ]
        outcome = search_subsets(
            batch.delta_paise,
            available,
            node_budget=node_budget,
            timeout_ms=timeout_ms,
            max_evidence=max_evidence,
            max_subset_size=max_subset_size,
        )
        window_used = stage
        if outcome.status in ("resolved", "ambiguous"):
            break
    if outcome is None:  # pragma: no cover - window_stages_days is validated non-empty
        raise RuntimeError("no window stages were searched")
    return outcome, window_used


def resolve(
    dataset: NormalizedDataset,
    candidates: list[CandidateBatch],
    pool_row_ids: list[str],
    *,
    node_budget: int = DEFAULT_NODE_BUDGET,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_evidence: int = DEFAULT_MAX_EVIDENCE,
    max_subset_size: int = DEFAULT_MAX_SUBSET_SIZE,
    window_stages_days: tuple[int | None, ...] = DEFAULT_WINDOW_STAGES_DAYS,
) -> LayerResult:
    """Run Layer 2 over Layer 1's unbalanced candidates.

    **Definite resolutions propagate before any ambiguity is declared.** Resolving one batch
    removes its rows from the pool, which shrinks every other batch's candidate set — and can
    turn a batch that looked ambiguous into one with a unique answer. So the resolve pass runs
    to a fixed point first, and only then is what remains classified.

    Doing it in one pass is both weaker and *wrong*: a row named in an early batch's ambiguity
    could still be claimed by a later batch's resolution, putting the same record in a group and
    an exception at once. The partition invariant's disjointness check caught exactly that.
    """
    if not window_stages_days:
        raise ValueError("window_stages_days must contain at least one stage")

    result = LayerResult()

    gateway_by_id = {row.row_id: row for row in dataset.gateway_rows}
    bank_by_id = {row.row_id: row for row in dataset.bank_rows}
    pool = [gateway_by_id[row_id] for row_id in pool_row_ids if row_id in gateway_by_id]

    claimed: set[str] = set()
    pending = sorted(candidates, key=lambda b: b.settlement_id)
    final: dict[str, tuple[SearchOutcome, int | None]] = {}

    # --- resolve to a fixed point -------------------------------------------------------
    while True:
        progressed = False
        still_pending: list[CandidateBatch] = []

        for batch in pending:
            outcome, window_used = _search_staged(
                batch,
                pool,
                claimed,
                node_budget=node_budget,
                timeout_ms=timeout_ms,
                max_evidence=max_evidence,
                max_subset_size=max_subset_size,
                window_stages_days=window_stages_days,
            )
            final[batch.settlement_id] = (outcome, window_used)

            if outcome.status != "resolved":
                still_pending.append(batch)
                continue

            (subset,) = outcome.solutions
            proposal = GroupProposal(
                group_id=f"grp_{batch.settlement_id}",
                layer=LAYER,
                record_ids=batch.member_row_ids
                + (batch.bank_row_id,)
                + batch.merchant_row_ids
                + subset,
                settlement_id=batch.settlement_id,
                bank_row_id=batch.bank_row_id,
                gateway_row_ids=batch.member_row_ids + subset,
                detail=(
                    f"delta {batch.delta_paise} closed by a unique subset of {len(subset)} pool "
                    f"rows (minimal size {outcome.subset_size}"
                    f"{', larger sizes unsearched' if outcome.larger_sizes_unsearched else ''}) "
                    f"within a {window_used if window_used is not None else 'causal-only'}-day "
                    f"window, found in {outcome.nodes_explored} nodes"
                ),
            )
            verdict = verifier.verify(
                proposal, gateway_by_id=gateway_by_id, bank_by_id=bank_by_id
            )
            if isinstance(verdict, ReconException):
                result.exceptions.append(verdict)
                continue

            result.groups.append(verdict)
            claimed.update(subset)
            progressed = True

        pending = still_pending
        if not progressed:
            break

    # --- classify what is left ----------------------------------------------------------
    for batch in pending:
        outcome, _ = final[batch.settlement_id]
        record_ids = batch.member_row_ids + (batch.bank_row_id,) + batch.merchant_row_ids
        at_risk = abs(batch.delta_paise)

        if outcome.status == "ambiguous":
            result.exceptions.append(
                ReconException(
                    exception_type=EX_AMBIGUOUS,
                    layer=LAYER,
                    record_ids=record_ids
                    + tuple(r for r in outcome.rows_involved if r not in claimed),
                    amount_at_risk_paise=at_risk,
                    detail=(
                        f"{outcome.solutions_found} distinct subsets of size "
                        f"{outcome.subset_size} each close delta "
                        f"{format_rupees(batch.delta_paise, prefix='Rs ')}, so the arithmetic "
                        "does not determine which rows settled. Refused rather than picking "
                        f"one. {len(outcome.rows_involved)} pool rows are implicated across "
                        "the candidate subsets."
                    ),
                    evidence=tuple(
                        SubsetEvidence(row_ids=subset, sum_paise=batch.delta_paise)
                        for subset in outcome.solutions
                    ),
                    evidence_found=outcome.solutions_found,
                    evidence_truncated=outcome.truncated,
                    evidence_complete=True,
                )
            )
        elif outcome.status == "exhausted":
            result.exceptions.append(
                ReconException(
                    exception_type=EX_SUBSET_SEARCH_EXHAUSTED,
                    layer=LAYER,
                    record_ids=record_ids,
                    amount_at_risk_paise=at_risk,
                    detail=(
                        f"search hit its {outcome.limit_hit} bound at subset size "
                        f"{outcome.subset_size} after {outcome.nodes_explored} nodes over "
                        f"{outcome.candidates_considered} candidates, with "
                        f"{outcome.solutions_found} subsets found so far - a lower bound, not a "
                        "total. Neither a match nor an ambiguity can be claimed from an "
                        "incomplete search."
                    ),
                    evidence=tuple(
                        SubsetEvidence(row_ids=subset, sum_paise=batch.delta_paise)
                        for subset in outcome.solutions
                    ),
                    evidence_found=outcome.solutions_found,
                    evidence_truncated=outcome.truncated,
                    evidence_complete=False,
                )
            )
        else:
            result.exceptions.append(
                ReconException(
                    exception_type=EX_UNCLASSIFIED,
                    layer=LAYER,
                    record_ids=record_ids,
                    amount_at_risk_paise=at_risk,
                    detail=(
                        f"no subset of {outcome.candidates_considered} eligible pool rows closes "
                        f"delta {format_rupees(batch.delta_paise, prefix='Rs ')}; the search was "
                        f"complete ({outcome.nodes_explored} nodes), so the explanation is not "
                        "in the pool."
                    ),
                )
            )

    # Unclaimed pool rows are deliberately NOT classified here. "Pending writeback" is a
    # terminal verdict, and Layer 2 is not the last layer: sweeping them up now would consume
    # rows Layer 3 needs as candidates. The harness classifies what survives the whole cascade.
    result.pool_row_ids = [r for r in pool_row_ids if r not in claimed]
    return result
