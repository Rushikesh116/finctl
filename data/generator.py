"""Synthetic dataset generator — `docs/SPEC.md` §3, §4.1, §5.

Emits three sources (merchant ledger, gateway records, bank statement) plus a *separate*
ground-truth labels file. Every record either names its true group or is explicitly labelled
unmatchable with a reason code.

Direction of dependency matters: this module imports record schemas **from** `core`. `core`
never imports this module, or the matcher could read the answers
(`tests/test_invariants.py::test_core_never_imports_ground_truth`).

**Determinism is a hard requirement** (invariant 4). Everything stochastic goes through one
seeded `random.Random`; nothing iterates a `set`, calls `hash()`, or derives output order
from a dict built out of a set. That is what makes the output byte-identical across processes
under different `PYTHONHASHSEED` values.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sys
import tomllib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.money import minor_unit_scale, pct_half_up
from core.records import BankRow, GatewayRow, Label, MerchantLedgerRow, SettlementLabel

PHASE = 1

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = REPO_ROOT / "data" / "scenarios.toml"
OUTPUT_DIR = REPO_ROOT / "data" / "generated"
HASH_MANIFEST = REPO_ROOT / "data" / "DATASET_HASHES.txt"

DATASET_SEEDS = {"dev_seed_11": 11, "holdout_seed_97": 97}

IST = timezone(timedelta(hours=5, minutes=30))

# Reason codes for unmatchable records. Every one must be registered in
# `core.records.REASON_CLASS`, which is what classifies it as `absent` (no partner exists —
# chase the feed) or `undetermined` (a partner exists but the data cannot say which — chasing
# the feed will not help). Label construction fails on an unregistered code.
REASON_BANK_ROW_ABSENT = "bank_row_absent"
REASON_ADJUSTMENT_NO_REF = "adjustment_without_reference"
REASON_REFUND_LATER_CYCLE = "refund_settles_in_later_cycle"
REASON_DISPUTE_LEG_UNSETTLED = "dispute_leg_unsettled"
REASON_POOL_DISTRACTOR = "unassigned_pool_distractor"

REASON_AMBIGUOUS_SUBSET = "ambiguous_subset_undetermined"
REASON_AMBIGUOUS_NO_KEY = "ambiguous_no_distinguishing_key"

# Which pathology a batch exhibits by virtue of its δ mechanism. Resolved before any row is
# constructed, since every row records the batch pathology it was built under.
MECHANISM_PATHOLOGY = {
    "on_hold_release_misdated": 10,
    "multiple_subsets_explain_delta": 7,
    "credit_without_parseable_utr": 2,
    "duplicate_reference_contamination": 2,
    "missing_bank_row": 8,
}


@dataclass
class GeneratedDataset:
    """One dataset: the three sources, plus ground truth kept deliberately separate."""

    name: str
    seed: int
    merchant_rows: list[MerchantLedgerRow] = field(default_factory=list)
    gateway_rows: list[GatewayRow] = field(default_factory=list)
    bank_rows: list[BankRow] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    settlement_labels: list[SettlementLabel] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.merchant_rows) + len(self.gateway_rows) + len(self.bank_rows)


def load_scenarios() -> dict:
    with SCENARIOS_PATH.open("rb") as handle:
        return tomllib.load(handle)


def semantic_expected_credit_paise(rows: list[GatewayRow]) -> int:
    """The **semantic** form of the settlement identity (SPEC §4).

        Σ captured − Σ refunds − Σ fee_base − Σ gst
        − Σ chargebacks + Σ representments
        − Σ adjustment_debits + Σ adjustment_credits

    Deliberately computed by *category* with explicit signs, rather than as
    `Σ credit − Σ debit − …`. The two are algebraically equal only while every row's category
    agrees with its direction, so comparing them catches exactly the bug that matters: a
    refund generated as a credit, or a chargeback as a credit, silently inverting a sign.

    `Σ gst` is a sum of stored per-row values, never a recomputation from the summed fee base
    — half-up rounding does not distribute over addition.

    Note there is no `+ Σ on_hold_released` term. The brief's identity carries one, but under
    the recon-row model a released row simply *is* its payment row appearing in the later
    batch, already counted in `Σ captured`. Adding a separate term would double-count it.
    """
    captured = sum(r.credit_paise for r in rows if r.type == "payment")
    refunds = sum(r.debit_paise for r in rows if r.type == "refund")
    fee_base = sum(r.fee_base_paise for r in rows)
    gst = sum(r.gst_paise for r in rows)

    adjustments = [r for r in rows if r.type == "adjustment"]
    chargebacks = sum(r.debit_paise for r in adjustments if r.dispute_id)
    representments = sum(r.credit_paise for r in adjustments if r.dispute_id)
    adj_debits = sum(r.debit_paise for r in adjustments if not r.dispute_id)
    adj_credits = sum(r.credit_paise for r in adjustments if not r.dispute_id)

    return (
        captured
        - refunds
        - fee_base
        - gst
        - chargebacks
        + representments
        - adj_debits
        + adj_credits
    )


def _ist_epoch(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).timestamp())


class _Generator:
    """One dataset's worth of construction. Single seeded RNG, no global state."""

    def __init__(self, name: str, seed: int, config: dict) -> None:
        self.name = name
        self.seed = seed
        self.cfg = config
        self.rng = random.Random(seed)

        settlement = config["settlement"]
        self.period_start = date.fromisoformat(config["dataset"]["period_start_ist"])
        self.period_end = date.fromisoformat(config["dataset"]["period_end_ist"])
        self.holidays = [date.fromisoformat(d) for d in settlement["bank_holidays_ist"]]
        self.cycle_days = settlement["settlement_cycle_days"]
        self.fee_bps = settlement["fee_bps"]
        self.gst_percent = settlement["gst_percent"]

        self.merchant: list[MerchantLedgerRow] = []
        self.gateway: list[GatewayRow] = []
        self.bank: list[BankRow] = []
        self.labels: list[Label] = []
        self.settlement_labels: list[SettlementLabel] = []

        self._counters: dict[str, int] = defaultdict(int)

    # -- identifiers ---------------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]:06d}"

    def _utr(self) -> str:
        """Shaped after the documented example `1597813219e1pq6w`."""
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        tail = "".join(self.rng.choice(alphabet) for _ in range(6))
        return f"{self.rng.randint(1_500_000_000, 1_899_999_999)}{tail}"

    # -- calendar ------------------------------------------------------------------------

    def _add_working_days(self, start: date, days: int) -> date:
        """T+N working days, skipping weekends and the configured bank holidays."""
        current, remaining = start, days
        while remaining > 0:
            current += timedelta(days=1)
            if current.weekday() < 5 and current not in self.holidays:
                remaining -= 1
        return current

    # -- money ---------------------------------------------------------------------------

    def _fee_and_gst(self, amount_paise: int, fee_base_override: int | None = None) -> tuple[int, int]:
        fee_base = (
            fee_base_override
            if fee_base_override is not None
            else pct_half_up(amount_paise, self.fee_bps, 10_000)
        )
        return fee_base, pct_half_up(fee_base, self.gst_percent, 100)

    # -- row builders --------------------------------------------------------------------

    def _payment_row(
        self,
        *,
        amount_paise: int,
        created_day: date,
        created_hour: int = 12,
        created_minute: int = 0,
        fee_base_override: int | None = None,
        settlement_id: str | None = None,
        settlement_utr: str | None = None,
        settled_at_utc: int | None = None,
        on_hold: bool = False,
        fx: tuple[str, int, int] | None = None,
    ) -> GatewayRow:
        fee_base, gst = self._fee_and_gst(amount_paise, fee_base_override)
        original_amount = original_currency = rate = None
        if fx is not None:
            original_currency, original_amount, rate = fx

        return self._record_gateway(
            GatewayRow(
                row_id=self._next("gw_"),
                type="payment",
                entity_id=self._next("pay_"),
                debit_paise=0,
                credit_paise=amount_paise,
                fee_base_paise=fee_base,
                gst_paise=gst,
                currency="INR",
                created_at_utc=_ist_epoch(created_day, created_hour, created_minute),
                on_hold=on_hold,
                settled=settlement_id is not None,
                order_id=self._next("order_"),
                order_receipt=self._next("receipt#"),
                settlement_id=settlement_id,
                settlement_utr=settlement_utr,
                settled_at_utc=settled_at_utc,
                method=self.rng.choice(["card", "netbanking", "upi", "wallet", "emi"]),
                international=fx is not None,
                amount_minor_original=original_amount,
                currency_original=original_currency,
                fx_rate_micros=rate,
            )
        )

    def _record_gateway(self, row: GatewayRow) -> GatewayRow:
        self.gateway.append(row)
        return row

    def _bank_credit(
        self, *, amount_paise: int, value_day: date, reference: str, narration: str
    ) -> BankRow:
        row = BankRow(
            row_id=self._next("bk_"),
            value_date_ist=value_day.isoformat(),
            narration=narration,
            reference=reference,
            credit_paise=amount_paise,
            debit_paise=0,
        )
        self.bank.append(row)
        return row

    def _merchant_order_for(
        self, payment: GatewayRow, *, customer_ref: str | None
    ) -> MerchantLedgerRow:
        row = MerchantLedgerRow(
            row_id=self._next("ml_"),
            kind="order",
            order_ref=payment.order_receipt or "",
            gateway_order_id=payment.order_id,
            amount_paise=payment.credit_paise,
            currency=payment.currency,
            minor_unit_scale=minor_unit_scale(payment.currency),
            issued_at_utc=payment.created_at_utc,
            customer_ref=customer_ref,
        )
        self.merchant.append(row)
        return row

    # -- labelling -----------------------------------------------------------------------

    def _label(
        self,
        row_id: str,
        source: str,
        pathologies: Sequence[int],
        *,
        group: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.labels.append(
            Label(
                row_id=row_id,
                source=source,  # type: ignore[arg-type]
                pathologies=sorted(set(pathologies)),
                true_group_id=None if reason else group,
                unmatchable=bool(reason),
                reason_code=reason,
            )
        )

    # -- planning ------------------------------------------------------------------------

    def _plan(self) -> list[str | None]:
        """One slot per batch: a mechanism name, `"missing_bank_row"`, or None for clean.

        Floors are constructed first and only then is the remainder filled by weight. A
        weighted draw cannot *guarantee* a floor, and at weight 0.03 M6 would be absent from
        a meaningful share of seeds — including, possibly, the holdout (SPEC §4.1).
        """
        settlement = self.cfg["settlement"]
        mechanisms = self.cfg["mechanism"]
        total = settlement["target_batches"]

        plan: list[str | None] = []
        for name in sorted(mechanisms):  # sorted, not dict order, for reproducibility
            plan.extend([name] * mechanisms[name]["min_instances"])

        wanted = round(total * settlement["delta_nonzero_fraction_target"])
        if len(plan) < wanted:
            names = sorted(mechanisms)
            weights = [mechanisms[n]["weight"] for n in names]
            plan.extend(self.rng.choices(names, weights=weights, k=wanted - len(plan)))

        # Pathology 8: batches that exist gateway-side with no bank credit at all. Not a
        # δ mechanism — there is no scalar to reconstruct against (SPEC §4.1 "M0").
        plan.extend(["missing_bank_row"] * 2)
        plan.extend([None] * (total - len(plan)))

        self.rng.shuffle(plan)
        return plan

    # -- batch construction --------------------------------------------------------------

    def _batch_size(self) -> int:
        # Weighted toward small batches with a long tail, so set reconstruction is exercised
        # at both ends without the record budget disappearing into one giant batch.
        return self.rng.choice([3, 4, 4, 5, 5, 5, 6, 6, 7, 8, 10, 12])

    def _amount(self) -> int:
        return self.rng.randrange(50_000, 25_000_000, 100)

    def run(self) -> GeneratedDataset:
        plan = self._plan()

        # Duplicate-reference pairs need a shared UTR, so pair their slots up deterministically.
        dup_slots = [i for i, m in enumerate(plan) if m == "duplicate_reference_contamination"]
        dup_partner: dict[int, str] = {}
        for left, right in zip(dup_slots[::2], dup_slots[1::2]):
            shared = self._utr()
            dup_partner[left] = shared
            dup_partner[right] = shared

        pathology_6_pool = list(self.cfg["pathology"]["6"]["fee_base_paise_candidates"])
        fx_currencies = list(self.cfg["pathology"]["12"]["currencies"])
        fx_rates = self.cfg["pathology"]["12"]["fx_rate_micros"]

        # One M5 case must produce more closing subsets than the evidence cap can record, so
        # the truncation path is exercised rather than assumed (SPEC §4.2).
        m5_slots = [i for i, m in enumerate(plan) if m == "multiple_subsets_explain_delta"]
        force_max_ambiguity = m5_slots[-1] if m5_slots else -1

        for index, mechanism in enumerate(plan):
            self._build_batch(
                index,
                mechanism,
                shared_utr=dup_partner.get(index),
                force_max_ambiguity=index == force_max_ambiguity,
                pathology_6_pool=pathology_6_pool,
                fx_currencies=fx_currencies,
                fx_rates=fx_rates,
            )

        self._build_standalone_pathologies()

        return GeneratedDataset(
            name=self.name,
            seed=self.seed,
            merchant_rows=self.merchant,
            gateway_rows=self.gateway,
            bank_rows=self.bank,
            labels=self.labels,
            settlement_labels=self.settlement_labels,
        )

    def _build_batch(
        self,
        index: int,
        mechanism: str | None,
        *,
        shared_utr: str | None,
        force_max_ambiguity: bool,
        pathology_6_pool: list[int],
        fx_currencies: list[str],
        fx_rates: dict[str, int],
    ) -> None:
        cfg_mech = self.cfg["mechanism"]
        settlement_id = self._next("setl_")
        group = f"grp_{settlement_id}"

        capture_day = self.period_start + timedelta(
            days=self.rng.randrange((self.period_end - self.period_start).days)
        )
        # Pathology 9: a late settlement dragged over a bank holiday.
        is_late = index % 11 == 3
        settle_day = self._add_working_days(capture_day, 5 if is_late else self.cycle_days)
        settled_at = _ist_epoch(settle_day, 18, 0)

        # Resolved BEFORE any row is built, because each row captures the batch pathologies at
        # construction time. Deciding it later left pathology 8 labelled as 1.
        #
        # A **union**, not an override. Every batch is a netting case (pathology 1) by
        # definition; a late-settling batch is *also* pathology 9; a batch with a δ mechanism
        # is *also* whatever that mechanism exhibits. The previous single-valued version made
        # these compete, so a batch that was both M1 and late reported only one of the two
        # — and the doubly-affected batches are the diagnostic ones (D-0016).
        pathology = [1]
        if is_late:
            pathology.append(9)
        if mechanism in MECHANISM_PATHOLOGY:
            pathology.append(MECHANISM_PATHOLOGY[mechanism])
        pathology = sorted(set(pathology))

        utr = shared_utr or self._utr()
        has_credit = mechanism != "missing_bank_row"

        # --- the joined members: rows that carry the UTR ---------------------------------
        # Each row carries its OWN pathology, not the batch's. Pathologies 3, 6 and 12 are
        # row-level properties (an edge timestamp, a half-paisa fee, an FX conversion), so
        # labelling them at batch granularity loses them entirely — which is exactly the bug
        # the per-dataset pathology-count test caught.
        joined: list[tuple[GatewayRow, int]] = []
        size = self._batch_size()
        for slot in range(size):
            # Pathology 3: 23:58 IST on the last day of the period = 18:28Z, two minutes
            # inside the 18:30Z cutoff. A correct implementation includes it (SPEC §3.4).
            if slot == 0 and index % 4 == 1:
                joined.append(
                    (
                        self._payment_row(
                            amount_paise=self._amount(),
                            created_day=self.period_end,
                            created_hour=23,
                            created_minute=58,
                            settlement_id=settlement_id,
                            settlement_utr=utr,
                            settled_at_utc=settled_at,
                        ),
                        pathology + [3],
                    )
                )
                continue

            # Pathology 6: a fee base whose 18% GST lands on a half-paisa.
            if slot == 1 and index % 3 == 0:
                joined.append(
                    (
                        self._payment_row(
                            amount_paise=self._amount(),
                            created_day=capture_day,
                            fee_base_override=pathology_6_pool[index % len(pathology_6_pool)],
                            settlement_id=settlement_id,
                            settlement_utr=utr,
                            settled_at_utc=settled_at,
                        ),
                        pathology + [6],
                    )
                )
                continue

            # Pathology 12: an international payment converted at an integer rate.
            if slot == 2 and index % 5 == 2:
                currency = fx_currencies[index % len(fx_currencies)]
                scale = minor_unit_scale(currency)
                original = self.rng.randrange(10, 5000) * scale
                joined.append(
                    (
                        self._payment_row(
                            amount_paise=self._amount(),
                            created_day=capture_day,
                            settlement_id=settlement_id,
                            settlement_utr=utr,
                            settled_at_utc=settled_at,
                            fx=(currency, original, fx_rates[currency]),
                        ),
                        pathology + [12],
                    )
                )
                continue

            joined.append(
                (
                    self._payment_row(
                        amount_paise=self._amount(),
                        created_day=capture_day,
                        settlement_id=settlement_id,
                        settlement_utr=utr,
                        settled_at_utc=settled_at,
                    ),
                    pathology,
                )
            )

        # --- the unassigned pool: rows in the batch with no settlement fields ------------
        pool: list[GatewayRow] = []
        explaining: list[list[str]] = []
        delta_rows: list[GatewayRow] = []

        if mechanism == "export_cutoff_skew":
            spec = cfg_mech[mechanism]
            count = self.rng.randint(spec["rows_short_min"], spec["rows_short_max"])
            pool = [
                self._payment_row(amount_paise=self._amount(), created_day=capture_day)
                for _ in range(count)
            ]
            delta_rows = pool

        elif mechanism == "on_hold_release_misdated":
            spec = cfg_mech[mechanism]
            lag = self.rng.randint(
                spec["release_lag_batches_min"], spec["release_lag_batches_max"]
            )
            # created_at from an earlier period, so a naive date window drops exactly the
            # rows that explain δ.
            earlier = capture_day - timedelta(days=7 * lag)
            pool = [
                self._payment_row(
                    amount_paise=self._amount(), created_day=earlier, on_hold=False
                )
                for _ in range(self.rng.randint(2, 4))
            ]
            delta_rows = pool

        elif mechanism == "multiple_subsets_explain_delta":
            spec = cfg_mech[mechanism]
            # k rows of the SAME amount, δ = 2 × one row's net. Every pair closes δ, giving
            # C(k,2) equally valid answers. Repeated equal amounts are ordinary in real
            # payment data, so this is the natural source of ambiguity, not a contrivance.
            k = (
                spec["identical_amount_rows_max"]
                if force_max_ambiguity
                else self.rng.randint(
                    spec["identical_amount_rows_min"], spec["identical_amount_rows_max"]
                )
            )
            twin_amount = self._amount()
            pool = [
                self._payment_row(amount_paise=twin_amount, created_day=capture_day)
                for _ in range(k)
            ]
            delta_rows = pool[:2]
            explaining = [
                [pool[i].row_id, pool[j].row_id]
                for i in range(len(pool))
                for j in range(i + 1, len(pool))
            ]

        elif mechanism == "pool_beyond_node_budget":
            spec = cfg_mech[mechanism]
            size = self.rng.randint(spec["pool_rows_min"], spec["pool_rows_max"])
            pool = [
                self._payment_row(amount_paise=self._amount(), created_day=capture_day)
                for _ in range(size)
            ]
            # δ is explained by a specific LARGE subset. Size, not pool size, is what puts it
            # out of reach: a search deepening by subset size must exhaust every smaller size
            # first, and sizes 1-4 over 44 candidates already cost ~150k combinations (D-0020).
            delta_rows = pool[: self.rng.randint(spec["delta_rows_min"], spec["delta_rows_max"])]

        # --- the bank credit ------------------------------------------------------------
        joined_rows = [row for row, _ in joined]
        true_members = joined_rows + delta_rows
        credit = semantic_expected_credit_paise(true_members)

        reference, narration = utr, f"NEFT-RAZORPAYSOFTWARE-UTR{utr}-STL"
        if mechanism == "credit_without_parseable_utr":
            reference = ""
            narration = self.rng.choice(
                ["NEFT CR-RAZORPAY SOFTWARE-SETTLEMENT", "IMPS/SETTLEMENT/CR", "RTGS CREDIT"]
            )


        bank_row = None
        if has_credit and credit > 0:
            bank_row = self._bank_credit(
                amount_paise=credit,
                value_day=settle_day,
                reference=reference,
                narration=narration,
            )

        # --- labels ---------------------------------------------------------------------
        # M5's records are labelled UNMATCHABLE even though a true pair exists, because the
        # data as given does not determine which pair. Labelling them matchable would score
        # the correct behaviour — refusing — as a missed match, penalising exactly what the
        # design asks for. Ground truth encodes what a correct system can *determine*, not
        # what the generator happens to know. See SPEC §4.3.
        #
        # M6 is the opposite: its δ *is* determined, just not findable within budget. Those
        # records stay matchable, so SUBSET_SEARCH_EXHAUSTED scores as an honest miss rather
        # than as a correct refusal. Giving up and declining are not the same thing.
        unmatchable_reason: str | None = None
        if mechanism == "multiple_subsets_explain_delta":
            unmatchable_reason = REASON_AMBIGUOUS_SUBSET
        elif mechanism == "missing_bank_row":
            unmatchable_reason = REASON_BANK_ROW_ABSENT

        for row, row_pathology in joined:
            self._label(
                row.row_id, "gateway", row_pathology, group=group, reason=unmatchable_reason
            )
            merchant_row = self._merchant_order_for(row, customer_ref=self._next("cust_"))
            self._label(
                merchant_row.row_id,
                "merchant",
                row_pathology,
                group=group,
                reason=unmatchable_reason,
            )

        delta_row_ids = [r.row_id for r in delta_rows]
        for row in pool:
            # Pool rows not needed by δ are distractors: they belong to no batch and widen
            # the search space, which is realistic and is meant to cost the search something.
            in_batch = row.row_id in delta_row_ids
            self._label(
                row.row_id,
                "gateway",
                pathology,
                group=group if in_batch else None,
                reason=unmatchable_reason
                or (None if in_batch else REASON_POOL_DISTRACTOR),
            )

        if bank_row is not None:
            self._label(bank_row.row_id, "bank", pathology, group=group, reason=unmatchable_reason)

        joined_delta = credit - sum(r.net_paise for r in joined_rows) if bank_row else 0
        self.settlement_labels.append(
            SettlementLabel(
                settlement_id=settlement_id,
                settlement_utr=utr,
                bank_row_id=bank_row.row_id if bank_row else None,
                mechanism=mechanism if mechanism != "missing_bank_row" else None,
                true_member_row_ids=[r.row_id for r in true_members],
                delta_paise=joined_delta,
                pool_row_ids=[r.row_id for r in pool],
                explaining_subsets=explaining,
            )
        )

    def _build_standalone_pathologies(self) -> None:
        """Pathologies that are not a property of a batch: 4, 5, 7, 11."""
        settled_batches = [
            label for label in self.settlement_labels if label.bank_row_id is not None
        ]

        # Pathology 4: a partial refund clawed back in the NEXT cycle. Its true group is the
        # batch that deducted it, not its parent payment (SPEC §3.8) — the payment link is a
        # record field, so the cross-batch linkage stays discoverable rather than labelled.
        for label in settled_batches[:3]:
            parent_id = label.true_member_row_ids[0]
            parent = next(r for r in self.gateway if r.row_id == parent_id)
            refund = self._record_gateway(
                GatewayRow(
                    row_id=self._next("gw_"),
                    type="refund",
                    entity_id=self._next("rfnd_"),
                    debit_paise=max(parent.credit_paise // 3, 100),
                    credit_paise=0,
                    fee_base_paise=0,
                    gst_paise=0,
                    currency="INR",
                    created_at_utc=parent.created_at_utc + 86_400,
                    settled=False,
                    payment_id=parent.entity_id,
                    order_id=parent.order_id,
                )
            )
            self._label(refund.row_id, "gateway", [4], reason=REASON_REFUND_LATER_CYCLE)

        # Pathology 5: both dispute legs are type="adjustment" sharing a dispute_id, debit
        # then credit. There is no `chargeback` value in the recon enum (SPEC §5.1).
        for _ in range(2):
            dispute_id = self._next("disp_")
            amount = self._amount()
            parent = self.gateway[self.rng.randrange(len(self.gateway))]
            for direction in ("debit", "credit"):
                leg = self._record_gateway(
                    GatewayRow(
                        row_id=self._next("gw_"),
                        type="adjustment",
                        entity_id=self._next("adj_"),
                        debit_paise=amount if direction == "debit" else 0,
                        credit_paise=amount if direction == "credit" else 0,
                        fee_base_paise=0,
                        gst_paise=0,
                        currency="INR",
                        created_at_utc=parent.created_at_utc + 172_800,
                        settled=False,
                        payment_id=parent.entity_id,
                        dispute_id=dispute_id,
                    )
                )
                self._label(leg.row_id, "gateway", [5], reason=REASON_DISPUTE_LEG_UNSETTLED)

        # Pathology 7: two customers, same amount, same day, no distinguishing key. The demo
        # centrepiece — a correct system declines and explains, it does not pick.
        for _ in range(2):
            amount = self._amount()
            day = self.period_start + timedelta(days=self.rng.randrange(20))
            for _ in range(2):
                twin = MerchantLedgerRow(
                    row_id=self._next("ml_"),
                    kind="order",
                    order_ref=self._next("receipt#"),
                    gateway_order_id=None,
                    amount_paise=amount,
                    currency="INR",
                    minor_unit_scale=100,
                    issued_at_utc=_ist_epoch(day, 14, 30),
                    customer_ref=None,
                )
                self.merchant.append(twin)
                self._label(twin.row_id, "merchant", [7], reason=REASON_AMBIGUOUS_NO_KEY)

        # Pathology 11: adjustment with dispute_id, order_id and payment_id ALL null.
        # dispute_id is the single field separating this from pathology 5 (SPEC §5.1).
        for _ in range(2):
            orphan = self._record_gateway(
                GatewayRow(
                    row_id=self._next("gw_"),
                    type="adjustment",
                    entity_id=self._next("adj_"),
                    debit_paise=self.rng.randrange(1000, 500_000, 100),
                    credit_paise=0,
                    fee_base_paise=0,
                    gst_paise=0,
                    currency="INR",
                    created_at_utc=_ist_epoch(self.period_end, 10, 0),
                    settled=False,
                )
            )
            self._label(orphan.row_id, "gateway", [11], reason=REASON_ADJUSTMENT_NO_REF)


def generate_dataset(name: str) -> GeneratedDataset:
    """Build `name` deterministically from its baked-in seed."""
    if name not in DATASET_SEEDS:
        raise ValueError(f"unknown dataset {name!r}; expected one of {sorted(DATASET_SEEDS)}")

    config = load_scenarios()
    return _Generator(name, DATASET_SEEDS[name], config).run()


# --- file emission -----------------------------------------------------------------------


def _write_csv(path: Path, rows: list, fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _write_json(path: Path, payload: object) -> None:
    # sort_keys so the bytes cannot depend on dict construction order.
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def emit(dataset: GeneratedDataset) -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = dataset.name

    merchant_path = OUTPUT_DIR / f"{stem}_merchant_ledger.csv"
    gateway_path = OUTPUT_DIR / f"{stem}_gateway_recon.json"
    bank_path = OUTPUT_DIR / f"{stem}_bank_statement.csv"
    labels_path = OUTPUT_DIR / f"{stem}_labels.json"

    _write_csv(
        merchant_path,
        dataset.merchant_rows,
        list(MerchantLedgerRow.__dataclass_fields__),
    )
    _write_json(gateway_path, [asdict(r) for r in dataset.gateway_rows])
    _write_csv(bank_path, dataset.bank_rows, list(BankRow.__dataclass_fields__))
    _write_json(
        labels_path,
        {
            # `unmatchable_class` is derived from `reason_code`, not stored on the record, but
            # it is written out so the labels file is self-describing to a reader who does not
            # have the REASON_CLASS registry to hand.
            "records": [
                {**asdict(label), "unmatchable_class": label.unmatchable_class}
                for label in dataset.labels
            ],
            "settlements": [asdict(label) for label in dataset.settlement_labels],
        },
    )
    return [merchant_path, gateway_path, bank_path, labels_path]


def dataset_paths(name: str) -> dict[str, Path]:
    """Where a dataset's four emitted files live.

    One place owns the naming convention. `core/normalize.py` takes explicit paths and never
    learns it, so nothing in the matcher depends on how datasets happen to be laid out.
    """
    if name not in DATASET_SEEDS:
        raise ValueError(f"unknown dataset {name!r}; expected one of {sorted(DATASET_SEEDS)}")
    return {
        "merchant": OUTPUT_DIR / f"{name}_merchant_ledger.csv",
        "gateway": OUTPUT_DIR / f"{name}_gateway_recon.json",
        "bank": OUTPUT_DIR / f"{name}_bank_statement.csv",
        "labels": OUTPUT_DIR / f"{name}_labels.json",
    }


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    names = sorted(DATASET_SEEDS) if "--all" in argv or not argv[1:] else argv[1:]

    manifest: list[str] = []
    for name in names:
        dataset = generate_dataset(name)
        paths = emit(dataset)
        print(f"{name}: {dataset.record_count} records, {len(dataset.settlement_labels)} batches")
        for path in paths:
            digest = sha256_of(path)
            manifest.append(f"{digest}  {path.relative_to(REPO_ROOT)}")
            print(f"  {digest}  {path.name}")

    HASH_MANIFEST.write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"\nwrote {HASH_MANIFEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
