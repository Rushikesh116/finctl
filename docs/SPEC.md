# FinCtl specification

> ## FROZEN — 2026-08-26, at the Phase 1 gate
>
> **Changing anything in this document now requires an entry in `DECISIONS.md` explaining why,
> and explicit approval** (`CLAUDE.md` → "Stop and ask before"). Everything downstream — the
> generator, the matcher, the harness, the metrics — is written against these definitions, so
> an unrecorded change here silently invalidates every result computed before it.
>
> Settled during Phase 1 and recorded: the identity form (D-0011), the scenario config format
> (D-0010), transfers out of scope (D-0012), records as dataclasses (D-0013), the
> M5-unmatchable / M6-matchable asymmetry (D-0014), and the `absent` / `undetermined` split
> (D-0015).
>
> **Still resting on undocumented gateway mechanics**, carried as *stated assumptions* that the
> README must name rather than as verified facts: §5 pathologies 10, 11 and 12 (Q-006, Q-007,
> Q-010), the netting identity itself (Q-005), the dispute reversal leg (Q-011), and the export
> cutoff skew that drives most of δ (Q-014).
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
| `reason_code` | `str \| None` | required when `unmatchable`; must be registered in `core.records.REASON_CLASS` |
| *`unmatchable_class`* | `"absent" \| "undetermined"` | **derived** from `reason_code`, never stored — see below |
| `pathology` | `int` | 1–12, so per-pathology accuracy is reportable |

Every record has exactly one label. There is no third state.

**Two kinds of unmatchable, and the exception queue must say different things about them:**

| Class | Meaning | Resolution | Reason codes |
|---|---|---|---|
| `absent` | **No true partner exists** in the data | Operational — chase the missing feed, or accept a write-off. **More data would fix it** | `bank_row_absent`, `adjustment_without_reference`, `refund_settles_in_later_cycle`, `dispute_leg_unsettled`, `unassigned_pool_distractor` |
| `undetermined` | **A partner exists, but the data cannot identify which** | A human decision, or a new distinguishing key. **More data will not help** — the rows are all present and simply do not discriminate | `ambiguous_subset_undetermined`, `ambiguous_no_distinguishing_key` |

Collapsed into one bucket, the queue would tell an operator to go hunting for a bank row that
is sitting right in front of them. The two also have different owners and different costs, so
they cannot share a resolution message (D-0015).

The class is **derived** from `reason_code` via the `core.records.REASON_CLASS` registry rather
than stored alongside it, so a code and its class cannot drift apart; an unregistered code
fails at `Label` construction. It is written into the labels file anyway, so that file is
self-describing to a reader without the registry to hand.

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

### 3.9 `SettlementLabel` (batch-level ground truth)

Record labels are per record; the δ mechanisms are per *batch*. So ground truth carries a
second, batch-level structure in the same labels file.

| Field | Type | Notes |
|---|---|---|
| `settlement_id` | `str` | `setl_…` |
| `settlement_utr` | `str \| None` | `None` when the settlement had no UTR at export time |
| `bank_row_id` | `str \| None` | `None` for pathology 8 — the credit does not exist |
| `mechanism` | `str \| None` | which `[mechanism.*]` was applied; `None` for a clean batch |
| `true_member_row_ids` | `list[str]` | the batch's actual membership |
| `delta_paise` | `int` | δ under the trivial `settlement_utr` join; `0` for a clean batch |
| `explaining_subsets` | `list[list[str]]` | **M5 only**: every subset of the pool that closes δ |

Two things this buys, beyond satisfying the floors:

- **Per-mechanism accuracy becomes reportable.** "Layer 2 resolves M1 but not M2" is a far
  more useful finding than a single aggregate rate, and it is the kind of thing the ablation
  table can show concretely.
- **The M5 refusal becomes checkable.** `explaining_subsets` is the full set of correct
  answers, so a Phase 3 test can assert the engine found *all* of them rather than stopping
  at two and declaring ambiguity. An engine that finds 2 of 21 and refuses is right by
  accident; one that finds 21 and refuses is right on purpose.

`mechanism` is an attribution, not a hint — it lives in the labels file, which `core/` cannot
import (invariant 2), so the matcher can never read which mechanism it is up against.

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

### 4.1 Why δ ≠ 0, how often, and what the search space is

**The failure this section exists to prevent.** If every gateway row in a batch carries
`settlement_id` and `settlement_utr`, then reconstructing a batch is a join, the identity
balances by construction, δ is always 0, the bounded subset search never fires, and Layer 2
is dead code. The hardest and most distinctive part of FinCtl would have nothing to do, and
the ablation table would show it bought nothing.

**What is *not* the fix.** Deleting or nulling a documented field to make the problem harder
would be rigging the dataset. `settlement_id` is documented and it groups rows into batches
cheaply and correctly. That is honest, and it *should* be easy — real reconciliation is
mostly a join, which is why it is mostly automatable.

**Where the difficulty actually is.** Two places, both real:

1. **Rows whose `settlement_id` is legitimately null.** A row not yet assigned to a
   settlement has neither `settlement_id` nor `settlement_utr` — that is the truth about
   it, not a gap. These form the **unassigned pool**.
2. **The gateway↔bank boundary.** `settlement_utr` is the only key shared across an
   organisational boundary, and the bank may truncate it, reuse it, or bury it in free-text
   narration.

**So the structure of the problem is:**

```
rows with settlement_id   -> grouped into batches trivially            (Layer 1)
rows with settlement_id=null -> the unassigned pool
δ(batch) = bank_credit − expected_credit(rows joined to that batch)
δ > 0  -> members of this batch are sitting in the unassigned pool
δ < 0  -> the join pulled in rows that belong elsewhere
search  -> find the subset of the *unassigned pool* summing to δ
```

**The search space is the unassigned pool, not the batch.** That is what makes the search
bounded and tractable at all: a 40-row batch is never enumerated, only the handful of
pending rows that could account for its shortfall.

#### The mechanisms

| # | Mechanism | How it makes δ ≠ 0 | Pathology |
|---|---|---|---|
| M1 | **Export cutoff skew** (primary) | Rows settled near the export cutoff appear with `settled=false` and null settlement fields because the gateway's row-level writeback lags the money movement — while the bank credit for their batch has already posted. The join yields an **incomplete** batch, so `δ > 0`. | 1, 3 |
| M2 | **On-hold release with misleading dates** | Released rows carry `created_at` from period *n* but belong to batch *n+k*. They enter the unassigned pool looking temporally wrong, so a naively date-windowed candidate filter excludes the very rows that explain δ. | 10 |
| M3 | **Credit with no parseable UTR** | The narration carries no extractable reference, so there is no join at all and `δ` = the entire credit. Resolved first by matching complete batch totals (a cheap scalar match over ~26 candidates), and only then by row-level search. | 2, 12 |
| M4 | **Duplicate reference contamination** | The same `reference` on two dates joins rows from two settlements into one candidate set, so `δ < 0`. Partitioning on `settled_at` resolves most; when the dates collide too it is a `DUPLICATE_REFERENCE` exception. | 2 |
| M5 | **Multiple distinct subsets explain δ** | More than one subset of the pool sums to δ, so **the arithmetic does not determine the answer.** Constructed from *k* equal-amount rows with δ = 2× that amount, giving `C(k,2)` closing subsets — repeated equal amounts are everywhere in real payment data, so this is the natural source of subset ambiguity rather than an invented one. | 7 (same principle) |

**M5 is the honest stopping condition, and it matters more than pool size.** When two
different subsets both close δ, picking the first one found is a coin flip dressed up as a
reconciliation. Layer 2 must **refuse** and emit `AMBIGUOUS`, exactly as Layer 3 refuses
when its best and second-best candidates fall within the margin. The refusal principle is
the same at both layers; only the evidence differs.

#### Explicitly *not* a search: absence (M0)

A batch with a `settlement_utr` and **no bank credit at all** (pathology 8) is
`MISSING_BANK_ROW` immediately. There is no scalar to reconstruct against, so there is
nothing to search for — hunting a credit that does not exist is unbounded and pointless.
**Distinguishing "δ ≠ 0, search" from "no counterpart, do not search" is a design
requirement, not an optimisation.** That is the "absence vs. mismatch" distinction pathology
8 tests, and getting it wrong shows up as a search timeout where a clean exception belonged.

#### The design target

**At least 30% of settlement batches must have δ ≠ 0 under the trivial `settlement_utr`
join.** The generator aims for 35–40% so the assertion is not marginal — a test that barely
passes is a test that flakes the moment a weight shifts.

Enforced by `tests/test_generator.py::test_delta_nonzero_fraction_meets_design_target`.
Weights live in `data/scenarios.toml` under `[mechanism.*]`.

**Every mechanism carries a `min_instances` floor that the generator guarantees in *both*
datasets** — constructed first, then the remainder filled by weight. Weights alone are not
sufficient, and M6 shows why: at weight 0.03 over 30 batches its expected count is ~0.9, so
a weight-only draw leaves a meaningful share of seeds with **zero** instances. If the
holdout is one of them, Phase 6's single shot reports an untested bound and the
`SUBSET_SEARCH_EXHAUSTED` column is empty with nothing to explain why.

| Mechanism | `min_instances`, per dataset | What it guarantees is observable |
|---|---|---|
| M1 export cutoff skew | 3 | Layer 2 succeeds on δ > 0 (rows in the pool) |
| M2 on-hold release, misdated | 2 | A naive date window is punished |
| M3 credit with no parseable UTR | 2 | Batch-total scalar match, then row search |
| M4 duplicate reference | 2 | Layer 2 handles δ < 0 (over-collection) |
| M5 multiple subsets explain δ | 2 (**≥1 exceeding the record cap**) | Layer 2 **refuses** — `AMBIGUOUS` |
| M6 pool beyond node budget | 1 | Layer 2 **gives up honestly** — `SUBSET_SEARCH_EXHAUSTED` |
| | **12 total → 12/30 = 40%** | |

The floor sum is what sets `target_batches`, not the other way round: 12 floors over 26
batches would force 46% and contradict the stated 40% target, so the batch count is 30. A
config test asserts that consistency, so the two cannot drift apart silently.

Search-space properties, also asserted:

| Property | Target | Why |
|---|---|---|
| Unassigned pool, per δ≠0 batch | 4–18 rows typical | Subset-sum is tractable within budget, so Layer 2 measurably *succeeds* |
| Total unassigned pool | ~10–15% of gateway rows | A realistic pending-writeback fraction |

The three outcome floors above are the point: a bounded search that only ever succeeds has
not demonstrated its bound, and one that only ever times out has not demonstrated its
search. **Every run, on either dataset, must show all three outcomes.**

### 4.2 Refusal evidence: what an `AMBIGUOUS` exception must record

A refusal is only worth anything if it is **auditable**. "More than one subset explained δ"
is a claim; the subsets themselves are evidence. A judge reading the exception should be
able to see the candidate sets side by side and check them, without querying anything.

So an `AMBIGUOUS` exception raised by Layer 2 records:

| Field | Type | Notes |
|---|---|---|
| `delta_paise` | `int` | the δ every recorded subset must close |
| `subsets_found` | `int` | **the true total, even when it exceeds the cap** |
| `subsets_recorded` | `int` | how many are in `subsets` below; ≤ the cap |
| `truncated` | `bool` | `subsets_found > subsets_recorded` |
| `subsets` | `list[SubsetEvidence]` | the candidate sets |

where each `SubsetEvidence` is:

| Field | Type | Notes |
|---|---|---|
| `row_ids` | `list[str]` | the gateway rows in this subset |
| `sum_paise` | `int` | **recorded explicitly**, so a reader can verify `sum_paise == delta_paise` without joining back to the records |

Three rules:

1. **Every recorded subset must independently sum to `delta_paise`.** A Phase 3 test asserts
   it on every `AMBIGUOUS` exception the engine emits. A recorded subset that does not close
   δ is not evidence of ambiguity, it is a bug in the search.
2. **The cap is `FINCTL_AMBIGUOUS_SUBSETS_RECORDED_MAX`, default 5**, so a pathological case
   cannot blow up the audit log. With `C(7,2) = 21` reachable from seven equal-amount rows,
   this is not hypothetical.
3. **Truncation must be visible.** `subsets_found` carries the real count and `truncated` is
   set. A log that says "2 subsets" when 21 existed understates the refusal and would let a
   reader conclude the case was nearly determined when it was nowhere close. This is the same
   principle as `SUBSET_SEARCH_EXHAUSTED`: a bound that hides its own operation is worse than
   no bound, because it looks like it worked.

`min_instances_exceeding_record_cap = 1` in `scenarios.toml` guarantees at least one case per
dataset where truncation actually fires, so the path is exercised rather than assumed.

### 4.3 Ground truth encodes what is *determinable*, not what the generator knows

Writing the generator forced this out into the open, and it changes what the metrics mean.

For an M5 batch the generator knows exactly which two pool rows went into the settlement.
But **the data as given does not determine them** — every one of the `C(k,2)` pairs closes δ
equally well. So if ground truth labelled those records matchable, an engine that correctly
**refused** would be scored as having missed a match. The metric would punish precisely the
behaviour the design asks for.

So: **M5 records are labelled `unmatchable`** with reason `ambiguous_subset_undetermined`.
Refusing is correct, and it scores as correct.

**M6 is the exact opposite, and the contrast is the point.** Its δ *is* determined — one
subset closes it — just not findable inside the node budget. Those records stay **matchable**,
so `SUBSET_SEARCH_EXHAUSTED` scores as an **honest miss**, not a correct refusal.

| | δ determined by the data? | Label | Correct engine behaviour | How it scores |
|---|---|---|---|---|
| M5 | **No** — many equal answers | `unmatchable` | refuse, `AMBIGUOUS` | correctly flagged |
| M6 | **Yes** — one answer, out of reach | matchable | `SUBSET_SEARCH_EXHAUSTED` | missed match |

**Giving up and declining are not the same thing**, and the metrics must not conflate them. An
exception that says "I could not determine this" is a success; one that says "I ran out of
budget" is an honest failure. Both are worth reporting, and reporting them as the same number
would hide the more interesting one.

The same rule governs pathology 7 (two customers, same amount, same day, no distinguishing
key): labelled `unmatchable` with `ambiguous_no_distinguishing_key`, because no correct system
could do better than decline.

#### This is a stress dataset, and the README must say so

The datasets are deliberately **pathology-dense**: all twelve pathologies appear at least
twice in ~500 records, and 30%+ of batches need work beyond a join. Production traffic is
nothing like this. So **the reported auto-match rate is not a production estimate**, and
claiming otherwise would be the most flattering lie available. What the numbers do support
is a comparison *between arms of the ablation on identical data*, which is what the
ablation table is for.

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
