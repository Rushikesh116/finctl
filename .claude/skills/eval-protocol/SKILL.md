---
name: eval-protocol
description: How FinCtl measures itself — the exact metrics block, the precise definition of every metric and denominator, the closed exception-type enum, cost accounting with verified token rates, the ablation table, and the rule that no number reaches a document unless a command printed it. Read this before running the harness, before writing or changing any metric, and before putting any number in README.md, METRICS.md or a pitch.
---

# Evaluation protocol

**The measurement is the product.** The grading bar for this track is "throughput plus
measured accuracy plus an honest exception list — one cherry-picked match proves
nothing." A matcher without honest metrics scores zero. That is why the harness is built
in Phase 2, before three of the four matching layers exist: every later improvement is
then measured against a *recorded* baseline instead of a remembered one.

## 1. The paste rule

**No number appears in any document unless a command produced it.**

- Every metric in `docs/METRICS.md`, `README.md`, or any pitch material is **pasted from
  stdout**, with the command and the git SHA written above it.
- A result you have not run is written `TBD`. Not an estimate, not "roughly", not a
  placeholder that looks like a measurement.
- Never retype a number you can pipe. Retyping is where a 93.4% becomes a 94.3%.
- Never claim a test passes without showing the command and its output.

## 2. Running it

```bash
make eval                     # dev + holdout + ablation; prints the metrics block
DEMO_MODE=1 make eval         # fixtures only: no network, no API key required
```

`DEMO_MODE=1 make eval` **must** complete with the network disabled. If it needs a key,
the fixture cache is incomplete and that is a bug, not an excuse.

**Holdout discipline.** `holdout_seed_97` is evaluated **once**, in Phase 6, and whatever
it prints is what ships — even if it is worse than dev. Touching it for anything else
requires explicit approval (CLAUDE.md → "Stop and ask before"). Iterating against a
holdout converts it into a training set and every number after that is a lie.

**Determinism check.** Two consecutive runs on the same seed and input must produce a
byte-identical audit log:

```bash
make eval && cp audit.log /tmp/a && make eval && diff /tmp/a audit.log && echo "replay ok"
```

A diff here means non-determinism has entered — most likely an uncached LLM response, a
dict iteration order, or a wall-clock value written into a hashed field.

## 3. Unit of measurement

Fixed once, so the same word never means two things:

- **record** — one row in one of the three sources, identified by `(source, row_id)`.
  `N = records processed` is the total across all three sources in the dataset.
- **match group** — a set of records the engine asserts describe the same money
  movement, **approved by the verifier**. An LLM proposal is not a match group until
  `core/verifier.py` re-checks its arithmetic independently.
- **auto-matched** — a record belonging to an approved match group.
- **exception** — a record in no approved group. Every record is in exactly one of the
  two states.

**Partition invariant:** `auto_matched + exception_records == N`, exactly, asserted in the
harness. A metrics block where these do not sum is reporting on a subset it did not tell
you about.

## 4. Metric definitions and denominators

| Metric | Numerator | Denominator |
|---|---|---|
| Auto-match rate | auto-matched records | `N` |
| Per-layer rate | records resolved at that layer | `N` |
| **False-match rate** | records in an approved group that disagrees with ground truth | **auto-matched** |
| Exception rate | exception records | `N` |
| Correctly flagged | exceptions that ground truth says are genuinely unmatchable | exceptions |
| Coverage / recall | auto-matched records whose group matches truth | records whose truth has a partner |
| Throughput | `N` | wall-clock seconds |
| LLM calls / 100 | LLM calls × 100 | `N` |
| Cost / 1000 | run cost × 1000 | `N` |

**False-match rate is precision, not coverage, and its denominator is auto-matched, not
`N`.** This is the number nobody else will report and the one that proves the measurement
is honest. A system can trivially reach 99% coverage by matching everything; the
false-match rate is what makes that visible.

A **false match** is either of: a record grouped with a record that is not its true
partner, or a record matched at all when ground truth labels it unmatchable. Both are
counted; they are not distinguished in the headline number, and both are itemised in the
exception detail.

An exception that ground truth says *was* matchable is a **missed match** (false
negative), not a correctly-flagged one. So `exceptions = correctly_flagged + missed_matches`.

**Rupees at risk** — the summed amount of exception records, counting each distinct money
movement **once**. Where one exception spans records in several sources describing the same
movement, the gateway amount is authoritative. Summing all three sources triple-counts and
inflates the figure by roughly 3×; a reviewer will spot that immediately.

## 5. The metrics block

`make eval` prints exactly this shape, and this exact text is what gets pasted into
`docs/METRICS.md` and `README.md`:

```
Dataset: holdout_seed_97        SHA: <git sha>      2026-xx-xx xx:xx
Records processed        512          Wall clock      8.4s
Auto-matched             478   93.4%   Throughput    61 rec/s
  Layer 1  exact         441   86.1%   LLM calls         14
  Layer 2  netting        24    4.7%   Calls / 100      2.7
  Layer 3  fuzzy           9    1.8%   Cost / 1000    Rs 3.20
  Layer 4  LLM+verified    4    0.8%
False matches              2   0.42%   <- precision, not coverage
Exceptions                34    6.6%   Rs 1,84,220 at risk
  correctly flagged       31   91.2%
  by type: AMBIGUOUS 9, MISSING_BANK_ROW 7, UNEXPLAINED_ADJ 6, ...
```

**Those numbers are illustrative placeholders from the brief, not results.** They are
reproduced here only to pin the *format*. Whatever a real run prints is what ships.

They are internally consistent, which is a useful format check: 441/512 = 86.1%,
478/512 = 93.4%, 2/478 = 0.42%, 34/512 = 6.6%, 31/34 = 91.2%, 512/8.4 = 61 rec/s,
14×100/512 = 2.7. If your block fails those arithmetic relations, a denominator is wrong.

## 6. Exception types — a closed enum

Layer 4 classifies into this set and no other. An unclassifiable record is
`UNCLASSIFIED`, which is a finding, not a category to hide in. `DRAFT` until the Phase 1
SPEC freeze.

| Type | Meaning | Pathology |
|---|---|---|
| `AMBIGUOUS` | two or more candidates within the score margin; refused on purpose | 7 |
| `MISSING_BANK_ROW` | gateway says settled, no bank credit exists | 8 |
| `MISSING_GATEWAY_ROW` | bank credit with no gateway batch | 8 |
| `DUPLICATE_REFERENCE` | reference reused across days, cannot disambiguate | 2 |
| `UNEXPLAINED_ADJ` | adjustment with no resolvable order or payment reference | 11 |
| `SUBSET_SEARCH_EXHAUSTED` | bounded search hit its node budget or timeout | 1, 3 |
| `TIMING_OUTSIDE_WINDOW` | plausible partner exists but falls outside the date window | 9 |
| `FX_UNRESOLVED` | multi-currency line whose INR conversion cannot be reproduced | 12 |
| `DISPUTE_UNRESOLVED` | chargeback or representment leg not yet closed | 5 |
| `ON_HOLD_UNRELEASED` | held balance with no observed release | 10 |
| `VERIFIER_REJECTED` | a proposal failed independent arithmetic re-check | — |
| `UNPARSEABLE_NARRATION` | bank narration no regex or LLM could parse | — |
| `UNCLASSIFIED` | escape hatch; a non-zero count here is a finding | — |

`SUBSET_SEARCH_EXHAUSTED` must always be **visible**. A bounded search that silently
drops overflow is worse than an unbounded one, because it looks like it worked.

## 7. Cost accounting

Per call, from `response.usage` — never estimated from character counts:

```
usd = (input_tokens  * USD_PER_MTOK_IN
     + output_tokens * USD_PER_MTOK_OUT) / 1_000_000
```

Cache reads bill at **10% of the base input rate**; batch requests at 50%.

Rates **verified 2026-08-26** from
`https://platform.claude.com/docs/en/about-claude/models/overview.md`:

| Model | Input $/MTok | Output $/MTok |
|---|---|---|
| `claude-opus-5` (FinCtl default) | $5 | $25 |
| `claude-sonnet-5` | $2 | $10 |
| `claude-haiku-4-5` | $1 | $5 |

The block reports **`Cost / 1000` in rupees**, which needs an FX rate. `FINCTL_USD_INR`
is deliberately **empty** in `.env.example`: with no rate set, the harness prints the USD
figure and `Rs TBD`. It must never print a plausible-looking INR number derived from a
guessed rate — see `docs/OPEN_QUESTIONS.md` Q-004.

**The LLM-calls curve is a headline, not a footnote.** Track calls per run across runs and
expect the number to *fall*, because Layer 4 writes regexes into `core/rules_cache.py`
and the regexes do the work from then on. The LLM writes rules; the rules do the work.

## 8. The ablation table

Printed alongside the metrics block on every `make eval`. It is the direct answer to the
"AI judgment, and where you chose *not* to use AI" criterion:

| Arm | Layers enabled | Reports |
|---|---|---|
| deterministic | 1 + 2 | auto-match, false-match, exceptions |
| + fuzzy | 1 + 2 + 3 | same three, as deltas |
| + LLM | 1 + 2 + 3 + 4 | same three, as deltas |

Report the false-match rate on **every** arm. An arm that raises auto-match while also
raising false matches is a regression being sold as an improvement, and the table is what
makes that impossible to miss.

## 9. Determinism, and why not `temperature=0`

Replay determinism comes from the **fixture cache**: every LLM response is written to
`fixtures/llm/` keyed by a hash of the prompt, so replaying a run needs no network and no
API key.

It does **not** come from `temperature=0`. On current Claude models — Opus 5, Sonnet 5,
Fable 5, Opus 4.7/4.8 — the `temperature` parameter has been **removed and returns HTTP
400**. Sending it does not pin determinism; it fails the request. This is a real
constraint the brief predates, recorded as `DECISIONS.md` D-0004 and Q-003.

Practical consequence: the cache is not an optimisation, it is the determinism mechanism.
A cache miss during a replay run must **fail loudly**, never fall through to a live call.

## 10. Treat a suspiciously good result as a bug

If auto-match exceeds 99%, or false matches come out at exactly zero, **assume
ground-truth leakage or a tolerance set too wide** and investigate before recording it.

First four things to check, in order:

1. Does anything under `core/` import `data` or `eval`?
   `tests/test_invariants.py::test_core_never_imports_ground_truth` should have caught it —
   confirm the test actually ran and did not skip.
2. Is a tolerance non-zero? δ tolerance must be exactly 0 paise.
3. Is the ambiguity margin zero, or is pathology 7 being matched instead of refused?
   Pathology 7 producing anything other than `AMBIGUOUS` is a precision failure.
4. Does `auto_matched + exception_records == N`? If not, records are being dropped
   silently, and the rate is computed over a subset.

Write what you find in `docs/WHAT_BROKE.md` with the metric on both sides of the fix.
