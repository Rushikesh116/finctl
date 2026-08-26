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

## No entries yet

Phase 0 was scaffolding; nothing has broken in code. Two things were *caught before* they
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
`eval-protocol` and `CLAUDE.md` all state the once-only rule; the Makefile quietly contradicted
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
