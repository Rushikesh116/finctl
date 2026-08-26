---
name: money-invariants
description: The money rules for FinCtl — integer paise only, rounding direction, GST-on-fee computation, sign conventions for refunds and chargebacks, the split-without-losing-paise helper, and the settlement balance identity. Read this before writing or reviewing any arithmetic on an amount, any comparison of two amounts, any percentage, any split or proration, and before changing a tolerance.
---

# Money invariants

Float drift in money arithmetic is the failure this project is most likely to suffer and
least likely to notice: it produces plausible numbers, so it survives review and shows up
as an unexplained δ of 1 paisa in a settlement that should have balanced. These rules
exist to make that impossible rather than unlikely.

## 1. Integer paise, end to end

**Money is `int` paise. Never a `float`. Never a `Decimal`-to-`float` round trip.**

- Parsing, arithmetic, storage, and comparison all happen in `int` paise.
- Formatting to rupees happens **only** in the presentation layer, at the last possible
  moment, and is never fed back into a computation.
- `₹4,83,271.44` is `48327144`. There is no other representation of it in the codebase.
- SQLite stores paise as `INTEGER`. Never `REAL`.

Two tests in `tests/test_invariants.py` enforce this on `core/money.py`: no `float`
anywhere in a signature, and no `float()` or `round()` call in the body. If you find
yourself wanting either, the arithmetic is wrong, not the rule.

`Decimal` is not banned outright — it is banned from *carrying* money. It may appear in a
one-way conversion that immediately quantises to `int` paise (an FX rate, say), and the
`int` is what continues.

## 2. Rounding direction

Only two rounding situations exist in this project, and both are integer-only.

**Percentage of an amount** — half-up, computed without leaving the integers:

```python
def pct_half_up(base_paise: int, numerator: int, denominator: int) -> int:
    """base * numerator / denominator, rounded half-up. Requires base_paise >= 0."""
    assert base_paise >= 0, "apply sign at the call site, not inside rounding"
    return (base_paise * numerator + denominator // 2) // denominator
```

Half-up, not banker's rounding: it is the convention a finance reviewer expects on Indian
tax arithmetic, and a reviewer who has to ask which rounding you used has already lost
confidence in the number. Sign is applied by the caller so floor division never skews a
negative. `PROJECT CONVENTION` — the Razorpay docs state no rounding direction (Q-002).

**Splitting a total across parts** — largest remainder, so paise are neither lost nor
invented:

```python
def split_with_remainder(total_paise: int, weights: Sequence[int]) -> list[int]:
    """Split total across len(weights) parts. Guarantees sum(result) == total_paise."""
```

Algorithm: floor each part at `total * w_i // W`, then hand the leftover
`total - Σ floor_i` paise, one each, to the parts with the largest true remainder
(`total * w_i % W`), ties broken by lowest index so the result is deterministic.

The post-condition `sum(result) == total_paise` is not a nicety — it is the whole point,
and it must be a property test over random inputs, not three hand-picked cases.

## 3. The fee/GST trap

**Read this before writing any netting arithmetic.** The word "fee" means two different
things in two Razorpay surfaces, and conflating them double-counts GST:

| Surface | Documented wording | Reading |
|---|---|---|
| Payment entity `fee` | "Fee (including GST) charged by Razorpay." | GST-**inclusive** |
| Payment entity `tax` | "GST charged for the payment." | the GST **inside** `fee` |
| Dashboard settlement break-up | `Payment - Adjustment - Tax - Fee` | Tax and Fee deducted **separately**, implying `fee` is GST-**exclusive** |

Under the first reading, net = `amount − fee`. Under the second, net = `amount − fee − tax`.
The two differ by exactly the GST. Getting this backwards shifts every settlement δ by
18/118 of the fee — small enough to look like a rounding bug, large enough to fail every
balance check.

**FinCtl's canonical schema sidesteps the ambiguity by not reusing the word.** Records
carry two unambiguous fields:

- `fee_base_paise` — the platform fee, **GST-exclusive**
- `gst_paise` — GST on that fee, `pct_half_up(fee_base_paise, 18, 100)`

and the netting identity subtracts both. Any ingestion of a real Razorpay Payment entity
must therefore **convert**, not copy: given a GST-inclusive `fee` and its `tax`,
`fee_base_paise = fee - tax` and `gst_paise = tax`. Write that conversion in one place,
with a test, and never inline it.

`UNVERIFIED` — which reading matches production is Q-002 in `docs/OPEN_QUESTIONS.md`.
The canonical schema above is correct under either reading; only the ingestion adapter
depends on the answer.

## 4. Sign conventions

**Amounts on records are non-negative magnitudes. Direction is a separate field.**

This mirrors the settlement recon report, which carries `debit` and `credit` as two
non-negative integers rather than one signed `amount`. Copying that shape means a sign
error cannot silently invert a refund.

| Concept | Stored as | Direction |
|---|---|---|
| Captured payment | `amount_paise > 0` | credit to merchant balance |
| Refund | `amount_paise > 0` | debit |
| Platform fee | `fee_base_paise > 0` | debit |
| GST on fee | `gst_paise > 0` | debit |
| Chargeback (dispute `lost`) | `amount_paise > 0` | debit |
| Representment (dispute `won`) | `amount_paise > 0` | credit |
| Adjustment | `amount_paise > 0` | **either** — carried per row |
| On-hold release | `amount_paise > 0` | credit |

**Adjustments can go either way.** The brief's identity writes `− Σ adjustments`, but a
recon row populates `debit` *or* `credit`, so an adjustment may be a credit. Treating all
adjustments as debits is a real bug that would show up as a δ of twice the adjustment.
Use the debit/credit split, not a signed subtraction.

Signs are applied when terms enter the identity, never baked into a stored amount.

## 5. The settlement balance identity

A gateway settles in **net batches**: one bank credit is the arithmetic result of many
transactions. Reconciling a bank line is therefore **set reconstruction against a single
scalar**, not row-to-row matching. That distinction is the core of this project.

**Semantic form** (the brief, §1):

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

**Recon-row form** (what the API actually hands you, per batch):

```
expected_credit = Σ credit − Σ debit − Σ fee_base − Σ gst_on_fee
```

These two must produce the identical integer on every generated batch. Assert it as a
test in Phase 1 — if they ever disagree, one of the two is a misreading of the domain and
the metrics built on top are worthless.

**δ and tolerance.** `δ = actual_bank_credit − expected_credit`, in paise.

- `δ == 0` → the whole batch reconciles at once. No search. This is the common case and
  it is why Layer 2 is cheap.
- `δ != 0` → search for the subset that explains δ, **bounded** by a node budget and a
  wall-clock timeout. Overflow becomes a typed exception; it never runs forever and it is
  never silently dropped.

**The tolerance on δ is exactly zero paise.** Money is exact. A non-zero tolerance is the
mechanism by which false matches enter a ledger while the match rate goes up — which is
precisely the trap the grading bar ("throughput plus measured accuracy plus an honest
exception list") is set to catch. If a batch is off by 1 paisa, that is a finding, not a
rounding allowance.

Amount tolerance in *fuzzy candidate generation* (Layer 3) is a different knob with a
different justification, and it defaults to 0 too (`FINCTL_AMOUNT_TOLERANCE_PAISE`).
Widening either one requires a `DECISIONS.md` entry and a before/after false-match rate.

## 6. Non-INR amounts

Not every currency has 100 sub-units: the Razorpay docs document JPY passing `295` for
¥295 (zero-decimal) and three-decimal currencies passing `295990`. So "paise" is a shorthand
for "integer minor units", and the scale is a property of the currency.

FinCtl stores, for an FX payment: the original `amount_minor` with its `currency` and that
currency's `minor_unit_scale`, **plus** the settled `amount_paise` in INR and the
`fx_rate_micros` used (rate × 10^6, an integer). Converting stores the result; it never
re-derives it, because re-deriving with a float is how an FX line drifts.

`UNVERIFIED` — how a real FX settlement represents the rate, the spread, and which side
rounds is Q-010. Pathology 12 is generated under a stated assumption, and the README says so.

## 7. Checklist before you commit money code

- [ ] No `float`, no `Decimal` carrying a money value, no `round()`.
- [ ] Every percentage goes through `pct_half_up`.
- [ ] Every split goes through `split_with_remainder`, with a `sum == total` property test.
- [ ] Fee and GST are two fields; neither is named `fee` alone.
- [ ] Amounts are non-negative; direction is a separate field.
- [ ] δ tolerance is 0; any tolerance elsewhere is justified in `DECISIONS.md`.
- [ ] Formatting to rupees happens once, in the presentation layer, and flows nowhere back.
