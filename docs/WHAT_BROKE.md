# What broke

Append-only, written **as it happens**, not reconstructed at the end. Real entries only —
this is not a retrospective narrative.

The submission form asks "what broke, and how you got out," and the organisers say they
read that answer first. So capture the numbers on both sides of a fix while you still
remember them.

Each entry:

```
## <date> — <one-line symptom>
**Symptom.** What was observed, with the number that looked wrong.
**Diagnosis.** The actual cause, and how it was found.
**Fix.** What changed.
**Metric before → after.** The specific measurement, both sides.
```

The four failures most likely to occur here, per the brief — worth recognising early rather
than debugging from scratch:

1. **Float drift in money arithmetic.** Produces plausible numbers, so it survives review
   and shows up as an unexplained δ of a paisa or two.
2. **Timezone boundary bugs.** Gateway timestamps are epoch UTC; bank value dates are IST
   with no time. See `SPEC.md` §3.4 for the exact interval rule and both failure directions.
3. **LLM non-determinism breaking replay.** Caught by the byte-identical-audit-log check,
   not by eyeballing output.
4. **Greedy matching inflating the match rate** while spot checks fail. This is why the
   false-match rate is reported on every ablation arm.

---

## Caught before they could break

Phase 0 was scaffolding; nothing had broken in code at that point. Two things were *caught before* they
could break, and they are recorded where they belong rather than dressed up as incidents
here:

- The `fee` / GST double-count ambiguity → `DECISIONS.md` D-0003, `OPEN_QUESTIONS.md` Q-002
- `temperature=0` returning HTTP 400 on the default model → D-0004, Q-003

Two more caught at the Phase 1 spec review, before any code was written against them:

- **A `pct_half_up` guard written as `assert` would have been strippable.** `python -O`
  removes asserts, and the demonstration is unambiguous: the assert version of the function
  returns `0` for an input of `-1` under `-O` — a silently wrong money value with no error
  raised. Now a `ValueError`, verified to still raise under `-O`, and structurally enforced
  by `tests/test_invariants.py::test_money_module_uses_exceptions_not_asserts`.
- **Half-up rounding does not distribute over addition**, so a batch-level GST check would
  have disagreed with the summed per-row GST by a paisa and looked like a data problem.
  `Σ gst_on_fee` is now defined as a sum of stored values (`SPEC.md` §4), with the exact
  25p+25p → 10 vs 9 case locked by `tests/test_money.py::test_gst_is_summed_not_recomputed`.

None of the four is a failure log entry, because none has produced a wrong number in a run.
If one does, it gets an entry with the metric on both sides.

---

## 2026-08-26 — `make eval` evaluated the holdout on every run

**Symptom.** The first Phase 2 `make eval` printed metrics blocks for **both** datasets. The
holdout is specified to be evaluated exactly once, in Phase 6.

**Diagnosis.** The Phase 0 Makefile defined `eval` as
`--dev $(DEV_DATASET) --holdout $(HOLDOUT_DATASET) --ablation`. It was written before the
harness existed, so the flag was inert for two phases and nothing surfaced it. `SPEC.md`,
`eval-protocol` and `ENGINEERING_RULES.md` all state the once-only rule; the Makefile quietly contradicted
all three, and the discipline lived only in prose.

**Fix.** `make eval` now runs the dev dataset only. The holdout moved to a separate
`make eval-holdout` target that announces what it is doing before it runs. A rule enforced
only by documentation is not enforced.

**Disclosure, because this matters more than the fix.** The holdout *was* evaluated, once, at
`a2687b1`: **auto-match 50.8%, false matches 0.00%, exceptions 236 of 480.** Recording it here
rather than deleting it — a holdout observation that goes unmentioned is worse than one that is
disclosed. Nothing has been tuned in response to it, and nothing will be: the number sits within
0.1pp of dev, so it carries no signal worth acting on even if I were willing to. Phase 6's
single evaluation stands as the reported result.

**Metric before → after.** No metric changed. What changed is that the leak can no longer
recur: `make eval` cannot touch the holdout.

---

## 2026-08-26 — the partition invariant had a blind spot that cancelled itself out

**Symptom.** A mutation test that moved a record into an approved group *while it was still
listed in an exception* did not trip the partition check. It was supposed to.

**Diagnosis.** The check was `auto_matched + exception_records == N`. A record counted in both
places is counted twice — and if some other record is simultaneously lost from both, the two
errors are equal and opposite. **The sum still reconciles over a set that is wrong in two
directions at once.** Exactly the class of error the invariant exists to catch, and the
invariant could not see it.

Worth noting how it was found: not by reading the code, but by a test written to prove the
*false-match detector* could fire. The blind spot was collateral.

**Fix.** Disjointness is now checked independently of the total: no record may appear in both a
match group and an exception, and the error names the offending ids. Two checks, because one
cannot express both properties.

**Metric before → after.** Baseline unchanged at 50.7% / 0.00% / 235 — the real run was never
in the failing state. What changed is that the check can now detect it, verified by
`tests/test_harness.py::test_a_record_cannot_be_both_matched_and_excepted`.

---

## 2026-08-26 — the subset search double-counted solutions, manufacturing false ambiguity

**Symptom.** Layer 2 reported `AMBIGUOUS` with `found=2` for a batch whose δ was uniquely
determined. Printing the two subsets showed them to be **the same subset, listed twice**.

**Diagnosis.** Solutions were counted mid-descent: on reaching `remaining == 0` the code recorded
a solution and deliberately kept descending, to catch supersets containing a zero-net row. But
every subsequent "skip this row" node also sees `remaining == 0` and scores the same subset
afresh, so a completed solution was counted once for every index after it.

**Why this was the worst possible direction to be wrong in.** `AMBIGUOUS` scores as a
*success* — a principled refusal (D-0014). So the bug converted resolvable batches into
apparent good behaviour: coverage fell, "refused" rose, and every headline number still looked
defensible. A metric-shaped bug that flatters the system is far harder to notice than one that
breaks it.

**Fix.** Count at the leaf, once per distinct include/exclude assignment. Locked by
`tests/test_settlement.py::test_solutions_are_never_double_counted`.

**Metric before → after.** Layer 2 on dev: 2 resolved / 3 refused → **6 resolved / 2 refused**,
with the refusals now being the two genuine M5 cases. Auto-match 50.7% → 62.7% at that step.

---

## 2026-08-26 — M6 was not hard, so the bounded search was never demonstrated stopping

**Symptom.** After the search was made competent, `pool_beyond_node_budget` **resolved** on dev.
Dev had zero exhausted cases, so `SUBSET_SEARCH_EXHAUSTED` appeared nowhere and the stopping
rule — the thing the brief specifically asks for — was untested.

**Diagnosis.** Phase 1 encoded M6's hardness as a large *pool* and asserted `pool_rows_min >= 40`
on the reasoning that less would fall to meet-in-the-middle. Wrong knob. For subset-sum with
iterative deepening, cost is driven by the **depth at which the answer sits**, not the number of
candidates: a 3-row answer among 44 candidates is ~14k nodes.

**Fix.** `delta_rows_min/max = 12..18` (D-0020). Reaching a 12-row answer means exhausting sizes
1–11 first, which is ~10^9 combinations. The Phase 1 test was rewritten to assert the right
property, and it now computes the combinatorial cost rather than eyeballing a pool size.

**Metric before → after.** `SUBSET_SEARCH_EXHAUSTED` on dev: **absent → 1 batch**, present in
both datasets. Datasets regenerated, so `DATASET_HASHES.txt` changed and the Phase 2 metrics row
is no longer comparable — visible in the Dataset SHA column, which is why that column exists.

**Uncomfortable part worth stating.** The wrong assertion passed for two phases and read as
rigorous while doing nothing. A test that asserts the wrong property is worse than no test,
because it buys confidence it has not earned.

---

## 2026-08-26 — a record ended up both matched and excepted

**Symptom.** The partition invariant's disjointness check fired: `gw_000200` was in a match group
and in an exception simultaneously.

**Diagnosis.** Making `AMBIGUOUS` name the rows the ambiguity is *about* introduced it. Naming a
row does not *claim* it, so a batch processed later could still resolve using that row — and
batches were processed in a single pass, in settlement-id order.

The deeper issue is that the single pass was simply wrong: resolving a batch removes its rows
from the pool, which shrinks every other batch's candidate set and can turn an apparently
ambiguous batch into a uniquely determined one. Order should not decide that.

**Fix.** Resolve to a **fixed point** — repeat the resolve pass until no further batch resolves —
and only then classify what remains. More correct and strictly more resolving.

**Metric before → after.** Layer 2 on dev: 5 resolved / 3 refused-or-exhausted →
**6 resolved / 3**, and auto-match 62.7% → **66.9%**. The disjointness check that caught it was
itself added in Phase 2 after a mutation test exposed the sum-only version as self-cancelling;
it has now caught a real bug twice.

---

# The recurring failure: a test that passes while asserting the wrong thing

Five separate instances now, which makes it the dominant failure mode of this project — more
common than any bug in the reconciliation logic itself. Grouping them here because the pattern
is the finding, not any individual case.

**The shape.** A check is written, it passes, and the passing is taken as evidence. But the
check is not testing the property anyone cares about — it tests a *proxy* for it, and the proxy
and the property come apart later. Nothing fails. The suite stays green. Confidence accrues
that was never earned, and it accrues *specifically* in the area the check was supposed to
protect, which is worse than having no check at all: an absent check leaves a known gap, a
wrong check closes it on paper.

**Why it dominated here.** Every instance sits at a boundary where the property is hard to
state directly, so a proxy is inviting:

- "the search is hard" → proxied by *pool size*
- "no record is lost or double-counted" → proxied by a *sum*
- "the holdout is evaluated once" → proxied by *a sentence in three documents*
- "pathology 7 is refused" → proxied by *a ground-truth label*
- "the deployed image reports its provenance" → proxied by *an environment configured to make the
  assertion pass*

Each proxy is reasonable. Each is also satisfiable without the property holding.

---

### 1. M6's hardness proxied by pool size

`assert oversized["pool_rows_min"] >= 40`, justified by reasoning about meet-in-the-middle.
**Passed for two phases while asserting nothing useful.** For subset-sum the cost is the depth
at which the answer sits, not the candidate count: a 3-row answer among 44 candidates is ~14k
nodes, so M6 *resolved* and `SUBSET_SEARCH_EXHAUSTED` never appeared on dev. The bounded
search — the thing the brief specifically asks for — shipped two phases without ever being seen
to stop.

**Now:** the test computes the combinatorial cost of reaching the true subset and asserts it
exceeds 10M, so it asserts the property rather than a stand-in for it.

### 2. The partition invariant proxied by a sum

`auto_matched + exception_records == N`. A record in **both** places is counted twice, and if
another is simultaneously lost the errors cancel exactly — the sum reconciles over a set wrong
in two directions at once. The invariant could not see the class of error it existed to catch.

**Found by accident**, by a mutation test written to prove the *false-match detector* could
fire. It has since caught a real bug in Layer 2.

**Now:** disjointness is checked independently of the total. Two checks, because one expression
cannot carry both properties.

### 3. The holdout rule proxied by documentation

`SPEC.md`, `eval-protocol` and `ENGINEERING_RULES.md` all stated that the holdout is evaluated once, in
Phase 6. The Phase 0 Makefile passed `--holdout` on every `make eval`. The flag was inert for
two phases because no harness existed to honour it, so nothing surfaced the contradiction, and
the first real `make eval` evaluated the holdout.

**Three documents agreeing is not enforcement.** The rule lived in prose and the prose could
not run.

**Now:** `make eval` cannot reach the holdout; `make eval-holdout` is a separate target that
announces itself. The one observation is disclosed above rather than deleted.

### 4. Pathology 7 proxied by its label

Two compounding versions of the same mistake.

`P7 46/46` looked like the demo centrepiece working. It was measuring 14 perfectly matchable M5
batch rows, because Phase 1 mapped mechanism M5 to pathology 7 — `SPEC.md` §4.1 says the two
share a *principle*, and that became a shared *label*. The population under measurement was
mostly not the pathology.

Underneath that, the pathology's own data could not exercise it: the twins had **zero**
same-amount gateway payments, so they were *unmatched*, not *ambiguous*. No correct engine could
have produced `AMBIGUOUS` against that data. The gate was unmeetable and the metric said 100%.

And a third, caught while fixing the first two: scoring a refusal as "did not match it" gives
full marks for never reaching the record. `P7 8/8` became `0/8` once the metric required a
*declared* `AMBIGUOUS`. A layer that does not exist was scoring 100% on the pathology it was
built to handle.

**Now:** M5 batches carry pathology 1 and their ambiguity is reported per mechanism; the twins
have gateway counterparts so a genuine 2×2 tie exists; and refusals are reported as two
permanent, separate lines with the strict definition.

### 5. The deployed environment proxied by a convenient one

The Dockerfile declares `ARG GIT_SHA=unknown` and bakes it to `ENV FINCTL_GIT_SHA`. On any
platform that builds the image itself — which is every platform, since none passes Docker build
args from a blueprint — that default is **always present**, so the provenance fallback checked
`FINCTL_GIT_SHA` first, found the non-empty string `unknown`, and returned it. `RENDER_GIT_COMMIT`
sat one entry later in the chain and was never read. The deployed service reported
`git_sha: unknown` — precisely the gap the fallback was written to close.

The test for it passed. It set `FINCTL_GIT_SHA` to **blank**, and blank is a state no deployment
produces. I had also "verified on Render's exact configuration" by running the container with
`-e FINCTL_GIT_SHA=` — I constructed the one environment in which the bug is invisible and called
it faithful. Two further reproduction attempts also missed, because the image under test had been
built *with* `--build-arg GIT_SHA=$(git rev-parse HEAD)` and therefore had a real SHA baked; the
bug only appears in an image built the way the platform builds it.

**What actually caught it:** the live URL. `/healthz` on the deployed service, read from outside.
Not the suite, which was green at 274 tests.

**Now:** the `unknown` sentinel is skipped rather than returned, so our own marker for *no answer*
cannot outrank a real one; a test asserts the deployed condition verbatim (sentinel baked in **and**
platform variable present) and is mutation-checked against the old code; and a companion test
confirms `unknown` is still returned when it genuinely is the only answer, so the skip is ordering
and not suppression. Verified by rebuilding with no build arg — `FINCTL_GIT_SHA=[unknown]` in the
image, `git_sha: 9d22c78` on `/healthz`.

**The lesson is narrower than "test the real thing" and worth stating exactly:** when a test needs
the environment configured, the configuration is part of what is under test. Setting it to whatever
makes the assertion pass is the same error as writing the assertion to match the output.

---

**What this changes about how the remaining phases are checked.** For each gate, the question
is not "does a test pass" but **"could this test pass while the property is false?"** Four
habits came out of it, all now in use:

1. **Mutation-test the check.** A guard that has never been seen to fail has not been shown to
   work. Every config guard added since is verified by breaking the config.
2. **Assert the property, not a stand-in.** Where the property is combinatorial, compute it —
   the M6 test now calculates the search cost instead of eyeballing a pool size.
3. **Prefer the strict reading of any metric.** Where "correct" could mean *avoided a wrong
   answer* or *gave the right answer for the right reason*, report the second. The first
   flatters exactly the components that do not exist yet.
4. **Reproduce in the environment that has the bug, not the one that is easy to build.** If a
   test or a manual check needs environment variables or build flags set, those settings are part
   of the claim. Added after #5, where three separate reproduction attempts all quietly removed
   the condition that caused the failure.

---

## 2026-08-26 — a change made to give Layer 3 work made the exception queue worse

**Symptom.** Layer 3 resolved **0** records. Investigating showed 42 of the 46 ledger rows
reaching it had their gateway counterpart already named in an exception, so no candidate existed.
Releasing those rows from batch-level exceptions looked like the fix.

**Diagnosis.** It gained Layer 3 nothing — the counterparts were still inside unresolved batches,
so there was still nothing to pair against — and it moved 38 records from a specific verdict
("this batch is unresolved, here is every record in it") into `UNCLASSIFIED`. Strictly less
information, in the file the track bar calls the deliverable.

The reasoning error was treating a record's presence in an exception as a *cascade artefact*
blocking a later layer, when it was a *fact*: attributing an order to a payment that is itself
unreconciled does not reconcile the order. The 42 are blocked upstream, correctly.

**Fix.** Reverted. Batch exceptions name their ledger rows again, and the reverted state is
commented at both call sites so the next person does not retry it.

**Metric before → after.** `UNCLASSIFIED` 4 → 42 → **0**. Layer 3's coverage contribution was 0
in every version; the release bought nothing and cost 38 records' worth of specificity.

**Caught by** the phase-4 `UNCLASSIFIED` ceiling on the first test run after the change — a gate
written two phases earlier, which activated automatically when `harness.PHASE` became 4.

---

## 2026-08-27 — `.env` was never loaded, so a working API key was invisible for five phases

**Symptom.** The user set `GEMINI_API_KEY` and the harness still reported `mode=offline` with a
stub proposing. No error.

**Diagnosis.** `.env.example` documented every variable, `.gitignore` excluded `.env`, and
`scripts/check_secrets.py` told anyone who tripped it to "move the value into .env and reference
it from os.environ" — and **nothing in the codebase ever read `.env`**. Every lookup was a bare
`os.environ.get`, so a key sitting in the file was invisible. The failure was silent: no error,
just a stub quietly standing in.

Two compounding effects. The key had been unreachable since Phase 0, so five phases of "no
credential exists" was partly self-inflicted. And the user's `.env` still carried
`FINCTL_LLM_MODEL=claude-opus-5` from before the provider swap, which would have sent a Claude
model string to Gemini.

**Fix.** A stdlib loader in `core/config.py`, called from every entry point and idempotent — a
loader that must be called in exactly one place is a loader that will be missed, which is how
this survived. Real environment variables win over the file, so `DEMO_MODE=0 make eval` overrides
it and a deployed environment's injected secrets are never shadowed by a stale file in the image.

**Metric before → after.** LLM mode `offline` → `live`; 0 real model responses → 2.

---

## 2026-08-27 — blind retry on 503 burned a 20-per-day API quota

**Symptom.** After the loader fix, the first cold run died on
`429 RESOURCE_EXHAUSTED: quota exceeded, limit: 20, model: gemini-3.7-flash` before reaching all
four narration shapes. The full cold run could not be completed.

**Diagnosis.** `gemini-3.7-flash` was returning `503 UNAVAILABLE: high demand` persistently. My
retry loop treated that as worth hammering with exponential backoff — and **every attempt spends
a unit of a 20-request daily allowance**. A capacity problem was converted into a quota problem.
One probe call alone took 82.4 seconds and 4 retries to succeed; the SDK also retries internally
beneath mine, multiplying it.

Two distinct defects: a per-day 429 was retried as if transient, and the provider's own
`retryDelay: 19s` hint was ignored in favour of a guess.

**Fix.** A 429 whose quota is per-day now fails immediately with that stated as the reason —
retrying cannot help. And `retryDelay` is honoured when the provider offers one.

**Metric before → after.** Cold run: incomplete, quota spent, 2 of 3 model-requiring narration
shapes reached. Not recoverable until the daily quota resets — which is itself the honest answer
about what running this costs.

**Worth stating plainly:** the 82-second first call and the 20-per-day ceiling are facts about
operating this system, not noise around it. They are reported in `METRICS.md` separately from
calls-per-100, because calls-per-100 is a cost measure and latency under load is not.
