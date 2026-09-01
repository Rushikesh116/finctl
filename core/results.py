"""Shared result types for the layer cascade.

Every layer produces the same three things — approved groups, typed exceptions, and candidates
handed on — so the types live here rather than in whichever layer happened to need them first.
A layer importing its result types from a sibling layer would make the cascade order load-bearing
in the import graph.

Exception type constants are the closed enum from `docs/skills/eval-protocol/SKILL.md` §6.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.money import Paise

__all__ = [
    "EXCEPTION_TYPES",
    "EX_AMBIGUOUS",
    "EX_DISPUTE_UNRESOLVED",
    "EX_DUPLICATE_REFERENCE",
    "EX_FX_UNRESOLVED",
    "EX_MISSING_BANK_ROW",
    "EX_MISSING_GATEWAY_ROW",
    "EX_ON_HOLD_UNRELEASED",
    "EX_SUBSET_SEARCH_EXHAUSTED",
    "EX_TIMING_OUTSIDE_WINDOW",
    "EX_UNCLASSIFIED",
    "EX_UNEXPLAINED_ADJ",
    "EX_UNPARSEABLE_NARRATION",
    "EX_VERIFIER_REJECTED",
    "CandidateBatch",
    "GroupProposal",
    "LayerResult",
    "MatchGroup",
    "ReconException",
    "SubsetEvidence",
]

EX_AMBIGUOUS = "AMBIGUOUS"
EX_MISSING_BANK_ROW = "MISSING_BANK_ROW"
EX_MISSING_GATEWAY_ROW = "MISSING_GATEWAY_ROW"
EX_DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
EX_UNEXPLAINED_ADJ = "UNEXPLAINED_ADJ"
EX_SUBSET_SEARCH_EXHAUSTED = "SUBSET_SEARCH_EXHAUSTED"
EX_TIMING_OUTSIDE_WINDOW = "TIMING_OUTSIDE_WINDOW"
EX_FX_UNRESOLVED = "FX_UNRESOLVED"
EX_DISPUTE_UNRESOLVED = "DISPUTE_UNRESOLVED"
EX_ON_HOLD_UNRELEASED = "ON_HOLD_UNRELEASED"
EX_VERIFIER_REJECTED = "VERIFIER_REJECTED"
EX_UNPARSEABLE_NARRATION = "UNPARSEABLE_NARRATION"
EX_UNCLASSIFIED = "UNCLASSIFIED"

EXCEPTION_TYPES = frozenset(
    {
        EX_AMBIGUOUS,
        EX_MISSING_BANK_ROW,
        EX_MISSING_GATEWAY_ROW,
        EX_DUPLICATE_REFERENCE,
        EX_UNEXPLAINED_ADJ,
        EX_SUBSET_SEARCH_EXHAUSTED,
        EX_TIMING_OUTSIDE_WINDOW,
        EX_FX_UNRESOLVED,
        EX_DISPUTE_UNRESOLVED,
        EX_ON_HOLD_UNRELEASED,
        EX_VERIFIER_REJECTED,
        EX_UNPARSEABLE_NARRATION,
        EX_UNCLASSIFIED,
    }
)
"""The enum is closed. An exception type outside it is a bug, not a new category."""


@dataclass(frozen=True, slots=True)
class SubsetEvidence:
    """One subset that closes δ, recorded so a refusal is auditable (`SPEC.md` §4.2).

    `sum_paise` is stored rather than derived so a reader can check it equals δ without
    joining back to the records.
    """

    row_ids: tuple[str, ...]
    sum_paise: Paise


@dataclass(frozen=True, slots=True)
class GroupProposal:
    """A layer's *claim* that these records form one reconcilable unit.

    Not a match. Only `core/verifier.py` turns a proposal into a `MatchGroup`, after
    independently recomputing the arithmetic. That boundary is what makes invariant 3
    structural rather than a promise: the module that proposes never approves.
    """

    group_id: str
    layer: int
    record_ids: tuple[str, ...]
    settlement_id: str | None
    bank_row_id: str | None
    gateway_row_ids: tuple[str, ...]
    confidence: int = 100
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MatchGroup:
    """An approved group. Only the verifier constructs these."""

    group_id: str
    layer: int
    record_ids: tuple[str, ...]
    settlement_id: str | None
    bank_row_id: str | None
    expected_credit_paise: Paise
    actual_credit_paise: Paise

    @property
    def delta_paise(self) -> int:
        return self.actual_credit_paise - self.expected_credit_paise


@dataclass(frozen=True, slots=True)
class ReconException:
    """A record set the engine declines to match, with why and what it is worth."""

    exception_type: str
    layer: int
    record_ids: tuple[str, ...]
    amount_at_risk_paise: Paise
    detail: str
    evidence: tuple[SubsetEvidence, ...] = ()
    evidence_found: int = 0
    evidence_truncated: bool = False
    evidence_complete: bool = True

    def __post_init__(self) -> None:
        if self.exception_type not in EXCEPTION_TYPES:
            raise ValueError(
                f"{self.exception_type!r} is not in the closed exception enum. Add it to "
                "core/results.py and eval-protocol §6 deliberately, or use UNCLASSIFIED and "
                "treat the count as the finding it is."
            )


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    """A batch that joined cleanly but did not balance — Layer 2's input.

    Carries δ so Layer 2 never re-derives it, and `settled_at_utc` so the search can apply the
    causal constraint that a row cannot settle before it was created.
    """

    settlement_id: str
    settlement_utr: str | None
    bank_row_id: str
    delta_paise: int
    member_row_ids: tuple[str, ...]
    merchant_row_ids: tuple[str, ...]
    settled_at_utc: int | None = None
    actual_credit_paise: Paise = 0


@dataclass
class LayerResult:
    groups: list[MatchGroup] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    candidates: list[CandidateBatch] = field(default_factory=list)
    pool_row_ids: list[str] = field(default_factory=list)

    @property
    def matched_record_ids(self) -> list[str]:
        return [row_id for group in self.groups for row_id in group.record_ids]

    def merge(self, other: LayerResult) -> None:
        """Fold a later layer's output in. Candidates are replaced, not appended: what the
        next layer receives is whatever this one could not settle."""
        self.groups.extend(other.groups)
        self.exceptions.extend(other.exceptions)
        self.candidates = list(other.candidates)
