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
