# FinCtl specification

> **Status: DRAFT.** This document is **frozen at the end of Phase 1**. After that,
> changing anything here requires an entry in `DECISIONS.md` explaining why, and explicit
> approval (`CLAUDE.md` → "Stop and ask before").
>
> Reviewed 2026-08-26. §4's identity form is settled (D-0011) and the scenario config format
> is settled (D-0010). Three pathologies still rest on undocumented mechanics and are marked
> inline: §5 pathologies 10, 11 and 12 (Q-006, Q-007, Q-010). Those are carried as **stated
> assumptions**, named in the README, not as verified facts.
>
> **Scope:** `transfer` rows — Route split-payment legs — are **out of scope** (D-0012). The
> generator emits none and the matcher does not pretend to handle them.

## 1. Domain model

Three sources describe the same money:

| Source | Contains | Shape on disk |
|---|---|---|
| **Merchant ledger** | Orders the merchant believes they sold, plus refunds they issued | CSV |
| **Payment gateway** | Payments captured, refunds processed, fees, GST on fees, chargebacks, adjustments, settlements | JSON (recon-report shaped) |
| **Bank statement** | Credits and debits that actually hit the current account | CSV |
| *(Ground truth)* | The true partner of every record, or an explicit unmatchable label with a reason | JSON, **separate file** |

The reason this is not a simple join: **gateways settle in net batches.** One bank credit
is the arithmetic result of many transactions netted together. So reconciling a bank line
is **set reconstruction against a single scalar**, not row-to-row matching. That
distinction is the core of the project.

Field names, units, ID prefixes, and enum values for the real gateway entities are
recorded — with quotes and source URLs — in `.claude/skills/razorpay-domain/SKILL.md`.
This document specifies FinCtl's *canonical internal* schema, which deliberately differs
in two places (see §3.5).

## 2. Identifiers and dependency direction

Record identity is `(source, row_id)`. `row_id` is stable across regenerations of the
same seed, so an audit log entry stays meaningful.

Record schemas are defined in **`core/records.py`**. `data/` and `eval/` import them
*from* `core/`; `core/` imports neither. That one-way arrow is what makes invariant 2
mechanically checkable rather than a matter of discipline
(`tests/test_invariants.py::test_core_never_imports_ground_truth`).

## 3. Record schemas

All amounts are `int` minor units. All UTC timestamps are `int` epoch seconds.

### 3.1 `MerchantLedgerRow`

| Field | Type | Notes |
|---|---|---|
| `row_id` | `str` | `ml_000123` |
| `kind` | `"order" \| "refund_issued"` | |
| `order_ref` | `str` | the merchant's own reference; maps to the gateway's `order_receipt` |
| `gateway_order_id` | `str \| None` | `order_…`; absent when the merchant never got one |
| `amount_paise` | `int` | non-negative magnitude |
| `currency` | `str` | ISO 4217 |
| `minor_unit_scale` | `int` | 100 for INR, 1 for JPY — see §3.6 |
| `issued_at_utc` | `int` | epoch seconds |
| `customer_ref` | `str \| None` | **`None` for pathology 7** — that absence is the point |

### 3.2 `GatewayRow`

Shaped after the settlement recon report, because that is the surface a real integration
reads. `type` is the documented four-value enum and nothing else.

| Field | Type | Notes |
|---|---|---|
| `row_id` | `str` | `gw_000123` |
| `type` | `"payment" \| "refund" \| "adjustment"` | the documented enum also has `transfer`, which is **out of scope** (D-0012). There is no `chargeback` value — see §5 pathology 5 |
| `entity_id` | `str` | `pay_…`, `rfnd_…` |
| `payment_id` | `str \| None` | set on `refund` rows and on both dispute legs (§5.1); `transfer` is out of scope (D-0012) |
| `order_id` | `str \| None` | |
| `order_receipt` | `str \| None` | joins to `MerchantLedgerRow.order_ref` |
| `debit_paise` | `int` | ≥ 0 |
| `credit_paise` | `int` | ≥ 0; exactly one of debit/credit is non-zero |
| `fee_base_paise` | `int` | **GST-exclusive** — see §3.5 |
| `gst_paise` | `int` | GST on that fee |
| `currency` | `str` | |
| `on_hold` | `bool` | |
| `settled` | `bool` | |
| `settlement_id` | `str \| None` | `setl_…` |
| `settlement_utr` | `str \| None` | the join key to the bank statement |
| `created_at_utc` | `int` | |
| `settled_at_utc` | `int \| None` | |
| `dispute_id` | `str \| None` | `disp_…`; how a chargeback surfaces |
| `method` | `str \| None` | `card`, `netbanking`, `wallet`, `upi`, `emi` |
| `international` | `bool` | |
| `amount_minor_original` | `int \| None` | FX only |
| `currency_original` | `str \| None` | FX only |
| `fx_rate_micros` | `int \| None` | rate × 10⁶, an integer — never a float |

### 3.3 `BankRow`

| Field | Type | Notes |
|---|---|---|
| `row_id` | `str` | `bk_000123` |
| `value_date_ist` | `str` | `YYYY-MM-DD`, **no time component** — see §3.4 |
| `narration` | `str` | free text, **untrusted input** |
| `reference` | `str` | as printed; **not unique** — pathology 2 |
| `credit_paise` | `int` | ≥ 0 |
| `debit_paise` | `int` | ≥ 0 |
| `balance_paise` | `int \| None` | running balance where the bank prints one |

`narration` is treated as hostile text everywhere. It reaches the LLM in Layer 4, so
prompt injection through it is in scope — and is contained by invariant 3, not by
sanitising the string.

### 3.4 The timezone asymmetry

**Gateway timestamps are epoch UTC. Bank statements are IST dates with no time.** This
asymmetry is a real source of bugs and it gets one explicit rule:

> An IST value date `D` covers the UTC half-open interval
> `[D−1 T18:30:00Z, D T18:30:00Z)`.

IST is UTC+05:30 with no DST. Both failure directions are real:

- Truncating a UTC timestamp to its UTC date puts anything from `18:30Z` to `24:00Z` on
  the *previous* IST day — i.e. every transaction between 00:00 and 05:29 IST.
- Writing a period cutoff as `…T23:59:59Z` instead of `…T18:30:00Z` pulls the next IST
  day's early transactions into the closing period.

Pathology 3 sits 2 minutes inside the boundary (23:58 IST = 18:28Z) precisely so a
correct implementation includes it and an off-by-one one does not.

### 3.5 Two deliberate deviations from the gateway schema

1. **`fee_base_paise` + `gst_paise` instead of `fee` + `tax`.** The gateway's Payment
   entity documents `fee` as GST-**inclusive** with `tax` being the GST inside it, while
   the dashboard settlement break-up deducts Tax and Fee **separately**. The two readings
   differ by exactly the GST. FinCtl refuses to reuse the ambiguous word: it stores a
   GST-exclusive base and its GST, and any real-API ingestion **converts**
   (`fee_base = fee − tax`, `gst = tax`) in one audited place. See
   `money-invariants` → "The fee/GST trap" and **Q-002**.

2. **`debit_paise`/`credit_paise` instead of a signed `amount`.** Mirrors the recon
   report, so a sign error cannot silently invert a refund.

### 3.6 Non-INR amounts

Not every currency has 100 minor units. FinCtl stores, for an FX payment, the original
`amount_minor_original` with its `currency_original` and that currency's
`minor_unit_scale`, **plus** the settled INR `credit_paise` and the integer
`fx_rate_micros` used. Conversion happens once and the result is stored; it is never
re-derived, because re-deriving with a float is how an FX line drifts.

### 3.7 `Label` (ground truth — separate file)

| Field | Type | Notes |
|---|---|---|
| `row_id` | `str` | |
| `source` | `"merchant" \| "gateway" \| "bank"` | |
| `true_group_id` | `str \| None` | records sharing a group describe the same money movement |
| `unmatchable` | `bool` | |
| `reason_code` | `str \| None` | required when `unmatchable` |
| `pathology` | `int` | 1–12, so per-pathology accuracy is reportable |

Every record has exactly one label. There is no third state.

### 3.8 Group semantics

**A group is one reconcilable unit, not a pairwise partner relation.** For settled rows the
unit is **the settlement batch and the bank line it produced** — potentially a hundred-plus
records sharing one `true_group_id`, because a bank credit either balances against its
constituent set or it does not.

**Exactly one group per record.** `true_group_id` is a single value, never a list, and never
a choice between two defensible answers. That constraint is what the next two rules protect.

**A refund's `true_group_id` is the settlement batch that deducted it — not its parent
payment.** Confirmed for pathology 4: a refund clawed back in the next cycle settles in
batch *n+1* while its payment settled in batch *n*, so the two hold **different**
`true_group_id` values. That is correct, not a modelling compromise — they hit the bank on
different days, as parts of different credits, and each credit must balance on its own.

The refund→payment link is `GatewayRow.payment_id`, **a record field the matcher can read**.
So it needs no ground-truth representation at all. Two things follow:

- Ground truth never has to arbitrate between two candidate groups for one record, so the
  labels file has no ambiguity to encode.
- The cross-batch linkage pathology 4 tests stays a **discoverable fact** rather than a
  leaked answer. A matcher that finds it has done real work; a matcher that reads it from
  labels would be cheating, and invariant 2 blocks that path anyway.

**A record whose counterpart is absent from the data gets no group.** It is labelled
`unmatchable=True` with a `reason_code`. This is what makes pathology 8 (feed gap)
measurable in the right direction: gateway rows with no bank credit are `unmatchable`, so
raising `MISSING_BANK_ROW` scores as **correctly flagged** rather than as a missed match
against a group no bank row could ever complete. "Absence vs. mismatch" is a distinction the
labels have to make, or the metric cannot.

## 4. The settlement balance identity

> **FROZEN on the separate-subtraction form (D-0011).** `fee_base` and `gst` are subtracted
> as two terms. This matches both the brief and the gateway's own dashboard break-up
> (`Payment - Adjustment - Tax - Fee`).
>
> The identity is nonetheless FinCtl's **construct, not a documented gateway contract** —
> Razorpay publishes no closed-form netting formula (Q-005). It is stated here so it can be
> argued with, and it is what the generator satisfies by construction. The README says so
> plainly: reported accuracy measures the matcher against *this* model of the domain.
>
> One conversion still depends on Q-002: an adapter ingesting a real Payment entity, whose
> `fee` is documented GST-inclusive, must compute `fee_base = fee − tax` and `gst = tax` in
> one audited place.

**Semantic form:**

```
expected_credit = Σ captured
                − Σ refunds
                − Σ fee_base
                − Σ gst_on_fee
                − Σ chargebacks
                − Σ adjustment_debits
                + Σ adjustment_credits
                + Σ on_hold_released
```

**Recon-row form** (what an API integration can actually compute per batch):

```
expected_credit = Σ credit − Σ debit − Σ fee_base − Σ gst_on_fee
```

**Both forms must produce the identical integer on every generated batch.** A Phase 1
test asserts it. If they ever disagree, one is a misreading of the domain and every metric
built on top is worthless.

**`Σ gst_on_fee` means `sum(row.gst_paise for row in batch)`** — a sum of values stored per
row, never `pct_half_up(Σ fee_base, 18, 100)`. Half-up rounding does not distribute over
addition, so the two differ by up to a paisa per row and the batch stops balancing. Worked
counter-example and the reasoning are in `money-invariants` → "GST is summed from stored
rows"; the case is locked by `tests/test_money.py::test_gst_is_summed_not_recomputed`.

Note the deviation from the brief's §1 formula: it writes `− Σ adjustments`, but a recon
row populates `debit` *or* `credit`, so an adjustment can be a **credit**. Treating all
adjustments as debits produces a δ of twice the adjustment.

**δ and tolerance.** `δ = actual_bank_credit − expected_credit`, in paise.

- `δ == 0` → the whole batch reconciles at once, no search. The common case.
- `δ ≠ 0` → **bounded** subset search for the records explaining δ. A node budget and a
  wall-clock timeout, both configurable, that dump to a typed exception on overflow rather
  than running forever. **That bounded search is the stopping rule.**

**The tolerance on δ is exactly zero paise.** Money is exact. A non-zero tolerance is the
mechanism by which false matches enter a ledger while the headline match rate improves.

## 5. The twelve pathologies

All twelve must appear **at least twice** in *both* datasets; a Phase 1 test asserts it.
Mix weights live in **`data/scenarios.toml`**, parsed with stdlib `tomllib` (D-0010) — TOML
rather than YAML so the weights keep their inline commentary without adding a dependency.

| # | Pathology | What it tests | Notes for the generator |
|---|---|---|---|
| 1 | Settlement netting — one credit covers N payments | Set reconstruction | The base case, not an edge case. Batch sizes should span 2 to ~100 |
| 2 | Duplicate bank reference reused across days | Key collision handling | Same `BankRow.reference` on two different `value_date_ist` — breaks Layer 1's primary key |
| 3 | Payment at 23:58 IST on the last day of the period | Timezone / period boundary | 18:28Z; 2 minutes inside the `18:30Z` cutoff — see §3.4 |
| 4 | Partial refund clawed back in the next cycle | Cross-batch linkage | Refund carries `payment_id`, but `settled_at_utc` falls in a later batch |
| 5 | Chargeback followed by a representment reversal | Signed adjustments | **No `chargeback` type exists.** Both legs are `type="adjustment"` with `dispute_id` set — see §5.1 |
| 6 | Fee plus 18% GST, rounded to paise | Integer money discipline | `gst = pct_half_up(fee_base, 18, 100)`; pick fee bases whose GST lands on a half-paisa |
| 7 | **Two customers, same amount, same day, no distinguishing key** | **Refusal to match** | `customer_ref = None` on both. **The demo centrepiece** |
| 8 | Bank row missing entirely (feed gap) | Absence vs. mismatch | Gateway says settled; no `BankRow` exists. Must not be confused with a mismatch |
| 9 | Late settlement, T+5 over a bank holiday | Date window elasticity | Documented cycle is T+2 working days, next working day after a holiday |
| 10 | On-hold balance released in a later batch | Carry-forward state | `on_hold` true in batch *n*, released in *n+k*. Release mechanics **UNVERIFIED** — Q-006 |
| 11 | Adjustment with no order reference | Genuine unresolvables | `type="adjustment"`, `order_id=None`, `payment_id=None`. Whether this is real is **UNVERIFIED** — Q-007 |
| 12 | International payment with FX conversion | Multi-currency | Non-INR original + INR settled + integer rate. Representation **UNVERIFIED** — Q-010 |

**Pathology 7 is the demo centrepiece.** A system that confidently matches an ambiguous
pair has great coverage and terrible precision. FinCtl must decline, flag it `AMBIGUOUS`,
and explain why.

### 5.1 How a dispute is represented (pathology 5)

The recon `type` enum has **no `chargeback` value** — a dispute surfaces via `dispute_id` on
a row. So both legs of a dispute are carried as `adjustment` rows, distinguished from each
other by direction and from everything else by `dispute_id`:

| Leg | `type` | Direction | `dispute_id` | `payment_id` |
|---|---|---|---|---|
| Chargeback (dispute `lost`) | `adjustment` | `debit_paise` = `amount_deducted` | set | the disputed payment |
| Representment reversal (dispute `won`) | `adjustment` | `credit_paise` = same amount | **same value** | same value |

Both legs are `adjustment` because a chargeback is money pulled from the merchant balance by
the network, not a refund the merchant issued — and `adjustment` is the only enum value that
carries a balance movement with no order of its own. The shared `dispute_id` is what links
the two legs across batches.

**`dispute_id` is the single field separating pathology 5 from pathology 11.** Both are
`type="adjustment"`:

| | `type` | `dispute_id` | `order_id` / `payment_id` | Correct outcome |
|---|---|---|---|---|
| Pathology 5 leg | `adjustment` | **set** | set | resolvable — link to the dispute |
| Pathology 11 | `adjustment` | `None` | `None` | `UNEXPLAINED_ADJ` exception |

A matcher that branches on `type` alone cannot tell them apart and will either resolve
nothing or resolve both. It must branch on `dispute_id`. **Generator and matcher agree on
exactly this contract**; a Phase 1 test asserts that every generated dispute leg carries a
non-null `dispute_id` and every pathology-11 row carries three nulls.

`UNVERIFIED` (Q-011): the docs document only `amount_deducted` on a `lost` dispute and are
silent on how the return leg is represented. This is a stated assumption, named in the README.

## 6. Metric definitions

Metric definitions, denominators, the closed exception-type enum, and the exact metrics
block are specified in **`.claude/skills/eval-protocol/SKILL.md`** §3–§6 and are frozen
together with this document. They live there rather than being restated here so the two
cannot drift apart.

The one structural rule worth repeating: **`auto_matched + exception_records == N`,
exactly**, asserted in the harness. A metrics block whose parts do not sum is reporting on
a subset it did not disclose.

## 7. Datasets

| Dataset | Seed | Size | Purpose |
|---|---|---|---|
| `dev_seed_11` | 11 | ~500 records | Iteration. Look at it as much as you like |
| `holdout_seed_97` | 97 | ~500 records | Evaluated **once**, in Phase 6. Whatever it prints is what ships |

Both are generated deterministically, so they are regenerated rather than committed —
`data/generated/` is gitignored. The freeze is enforced by a **committed SHA-256 manifest**
(`data/DATASET_HASHES.txt`): regenerating and comparing detects drift, so "frozen" is a
checkable claim rather than a promise (D-0007).
