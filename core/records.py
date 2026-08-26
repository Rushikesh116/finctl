"""Canonical record schemas — `docs/SPEC.md` §3.

`core` owns these; `data` and `eval` import them *from* here. The arrow points one way, which
is what makes invariant 2 mechanically checkable rather than a matter of discipline
(`tests/test_invariants.py::test_core_never_imports_ground_truth`).

Frozen dataclasses rather than pydantic models: these are internal records constructed
millions of times across a run, and `slots=True` frozen dataclasses are the cheap, obvious
choice. Pydantic earns its keep at the *boundaries* — the LLM proposal schema in Phase 5 and
the API response models in Phase 6 — where untrusted input needs validating (D-0013).

Validation lives in `__post_init__` and **raises**; it is never an `assert`, for the same
reason the money guards are not (`python -O` strips them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.money import Paise

__all__ = [
    "REASON_CLASS",
    "BankRow",
    "GatewayRow",
    "GatewayRowType",
    "Label",
    "MerchantLedgerRow",
    "SettlementLabel",
    "Source",
    "UnmatchableClass",
]

# The documented recon `type` enum also carries `transfer` (Route split-payment legs), which
# is out of scope — no fetched page defines its settlement semantics (D-0012, Q-008).
GatewayRowType = Literal["payment", "refund", "adjustment"]
Source = Literal["merchant", "gateway", "bank"]

UnmatchableClass = Literal["absent", "undetermined"]
"""Why a record has no match — and these two need different things said about them.

* `absent` — **no true partner exists** anywhere in the data. The resolution is operational:
  chase the missing feed, or accept a genuine write-off. More data would fix it.
* `undetermined` — **a partner exists, but the data cannot identify which one.** The
  resolution is a human decision or a new distinguishing key. Chasing the feed will not help,
  because the rows are already all there; they simply do not discriminate.

Collapsing these into one bucket would have the exception queue tell an operator to go
looking for a bank row that is sitting right in front of them.
"""

REASON_CLASS: dict[str, UnmatchableClass] = {
    # --- absent: the counterpart is not in the data at all ---
    "bank_row_absent": "absent",
    "adjustment_without_reference": "absent",
    "refund_settles_in_later_cycle": "absent",
    "dispute_leg_unsettled": "absent",
    "unassigned_pool_distractor": "absent",
    # --- undetermined: the counterpart is present but not identifiable ---
    "ambiguous_subset_undetermined": "undetermined",
    "ambiguous_no_distinguishing_key": "undetermined",
}
"""The single source of truth mapping a reason code to its class.

`Label` derives its class from this rather than storing a second field, so a code and its
class cannot drift apart, and an unregistered code fails loudly at construction.
"""


def _require_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be a non-negative magnitude, got {value}")


@dataclass(frozen=True, slots=True)
class MerchantLedgerRow:
    """An order the merchant believes they sold, or a refund they issued."""

    row_id: str
    kind: Literal["order", "refund_issued"]
    order_ref: str
    gateway_order_id: str | None
    amount_paise: Paise
    currency: str
    minor_unit_scale: int
    issued_at_utc: int
    # None is the whole point for pathology 7: two customers, no distinguishing key.
    customer_ref: str | None = None

    def __post_init__(self) -> None:
        _require_non_negative("amount_paise", self.amount_paise)


@dataclass(frozen=True, slots=True)
class GatewayRow:
    """One settled or pending transaction, shaped after the settlement recon report.

    Direction is carried by which of `debit_paise`/`credit_paise` is populated, mirroring the
    recon report rather than inventing a signed amount — a sign error cannot then silently
    invert a refund (D-0008).

    `fee_base_paise` is **GST-exclusive** and `gst_paise` is its GST. The gateway's own `fee`
    field is documented GST-*inclusive*, so FinCtl refuses to reuse the ambiguous word
    (D-0003, Q-002).
    """

    row_id: str
    type: GatewayRowType
    entity_id: str
    debit_paise: Paise
    credit_paise: Paise
    fee_base_paise: Paise
    gst_paise: Paise
    currency: str
    created_at_utc: int
    on_hold: bool = False
    settled: bool = False
    payment_id: str | None = None
    order_id: str | None = None
    order_receipt: str | None = None
    # Null when the row is not yet assigned to a settlement. That is the truth about it, not
    # a gap — these rows form the unassigned pool that Layer 2 searches (SPEC §4.1).
    settlement_id: str | None = None
    settlement_utr: str | None = None
    settled_at_utc: int | None = None
    dispute_id: str | None = None
    method: str | None = None
    international: bool = False
    amount_minor_original: int | None = None
    currency_original: str | None = None
    fx_rate_micros: int | None = None

    def __post_init__(self) -> None:
        for name in ("debit_paise", "credit_paise", "fee_base_paise", "gst_paise"):
            _require_non_negative(name, getattr(self, name))
        if self.debit_paise and self.credit_paise:
            raise ValueError(
                f"{self.row_id}: exactly one of debit_paise/credit_paise may be non-zero, "
                f"got debit={self.debit_paise} credit={self.credit_paise}"
            )
        if self.settlement_utr and not self.settlement_id:
            raise ValueError(
                f"{self.row_id}: has a settlement_utr but no settlement_id, which cannot "
                "happen — a UTR is issued against a settlement"
            )

    @property
    def net_paise(self) -> Paise:
        """This row's contribution to its batch's expected credit, in the recon-row form."""
        return self.credit_paise - self.debit_paise - self.fee_base_paise - self.gst_paise


@dataclass(frozen=True, slots=True)
class BankRow:
    """A credit or debit that actually hit the current account.

    `value_date_ist` has no time component — the gateway's timestamps are epoch UTC and the
    bank's are IST dates. That asymmetry is a real source of bugs and gets an explicit
    interval rule in SPEC §3.4.

    `narration` is untrusted text. It reaches the LLM in Layer 4, so prompt injection through
    it is in scope, and is contained by the verifier boundary rather than by sanitising.
    """

    row_id: str
    value_date_ist: str
    narration: str
    reference: str
    credit_paise: Paise
    debit_paise: Paise
    balance_paise: Paise | None = None

    def __post_init__(self) -> None:
        _require_non_negative("credit_paise", self.credit_paise)
        _require_non_negative("debit_paise", self.debit_paise)
        if self.credit_paise and self.debit_paise:
            raise ValueError(
                f"{self.row_id}: a bank line is a credit or a debit, not both"
            )


@dataclass(frozen=True, slots=True)
class Label:
    """Ground truth for one record — SPEC §3.7. Exactly one per record, no third state.

    Lives in the labels file. Nothing under `core/` may read it (invariant 2); this class is
    the *shape* of ground truth, not ground truth itself.
    """

    row_id: str
    source: Source
    pathology: int
    true_group_id: str | None = None
    unmatchable: bool = False
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.unmatchable:
            if self.true_group_id is not None:
                raise ValueError(f"{self.row_id}: unmatchable but carries a group")
            if not self.reason_code:
                raise ValueError(f"{self.row_id}: unmatchable needs a reason_code")
            if self.reason_code not in REASON_CLASS:
                raise ValueError(
                    f"{self.row_id}: reason_code {self.reason_code!r} is not in REASON_CLASS, "
                    "so its class is undefined. Register it as 'absent' or 'undetermined'."
                )
        elif self.true_group_id is None:
            raise ValueError(
                f"{self.row_id}: matchable records need a true_group_id; a record whose "
                "counterpart is absent should be unmatchable with a reason (SPEC §3.8)"
            )
        if not 1 <= self.pathology <= 12:
            raise ValueError(f"{self.row_id}: pathology {self.pathology} outside 1..12")

    @property
    def unmatchable_class(self) -> UnmatchableClass | None:
        """Derived, never stored — so it cannot disagree with `reason_code`."""
        return REASON_CLASS[self.reason_code] if self.reason_code else None


@dataclass(frozen=True, slots=True)
class SettlementLabel:
    """Batch-level ground truth — SPEC §3.9.

    `mechanism` is an attribution for scoring, not a hint: it lives in the labels file, so the
    matcher can never read which δ mechanism it is up against.

    `explaining_subsets` holds *every* subset of the pool that closes δ, which is what makes
    an M5 refusal checkable — an engine that finds 2 of 21 and refuses is right by accident.
    """

    settlement_id: str
    settlement_utr: str | None
    bank_row_id: str | None
    mechanism: str | None
    true_member_row_ids: list[str]
    delta_paise: int
    # Every unassigned row offered to this batch, including distractors that belong to no
    # batch. This is the search space Layer 2 actually faces, so it is what a bound should be
    # judged against — and it lets the pool-realism assertion exclude the deliberately
    # oversized M6 case instead of averaging it in.
    pool_row_ids: list[str] = field(default_factory=list)
    explaining_subsets: list[list[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.explaining_subsets and self.delta_paise == 0:
            raise ValueError(
                f"{self.settlement_id}: subsets recorded for a batch with δ == 0, so there "
                "was nothing to explain"
            )
