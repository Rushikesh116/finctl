# Metrics

Raw pasted stdout of `make eval`, with the git SHA and timestamp of the run above each
block. Nothing in this file is typed by hand — every number is pasted from a command's
output. A result that has not been run is `TBD`. See
`.claude/skills/eval-protocol/SKILL.md` for the paste rule and metric definitions.

---

## Phase 2 baseline — Layer 1 (exact matching) only

The number every later phase is measured against. Recorded before anything was tuned.

```
$ git rev-parse --short HEAD
ab178c5
$ date -u '+%Y-%m-%d %H:%M UTC'
2026-08-26 15:57 UTC
$ make eval
Dataset: dev_seed_11  data 371df9be   SHA: ab178c5   2026-08-26 15:57
Records processed         477          Wall clock    0.003s
Auto-matched              242    50.7%   Throughput  177455 rec/s
  Layer 1  exact            242    50.7%
  Layer 2  netting           --       --   not built yet
  Layer 3  fuzzy             --       --   not built yet
  Layer 4  LLM+verified      --       --   not built yet
False matches               0    0.00%   <- precision, not coverage
Exceptions                235    49.3%      Rs 29,56,629.16 at risk
  correctly flagged       116    49.4%
  missed matches          119    50.6%
  by type: UNCLASSIFIED 193, MISSING_BANK_ROW 40, MISSING_GATEWAY_ROW 2
  by class: absent 70, undetermined 46
LLM calls                   0          Calls / 100  0.0%
Cost / 1000            Rs TBD          USD 0.000000 total
Audit ledger               33 entries   head b6a2c5f69534
By mechanism  (delta != 0 batches; ground-truth attribution. refused is a SUCCESS, exhausted is an honest failure)
  credit_without_parseable_utr       2 batches  resolved 0  refused 0  exhausted 0  unclassified 0  MISSING_BANK_ROW 2
  duplicate_reference_contamination  2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  export_cutoff_skew                 3 batches  resolved 0  refused 0  exhausted 0  unclassified 3
  multiple_subsets_explain_delta     2 batches  resolved 0  refused 0  exhausted 0  unclassified 2
  on_hold_release_misdated           2 batches  resolved 0  refused 0  exhausted 0  unclassified 2
  pool_beyond_node_budget            1 batch    resolved 0  refused 0  exhausted 0  unclassified 1
FINDING  UNCLASSIFIED holds 193 records (82.1% of exceptions). Target 0 by Phase 5.
         Every record in it has a home in the enum; the count is a measure of layers not yet built, not of records that defy classification.
By pathology  (records carry >=1, so these OVERLAP and do not sum to 477)
  P1   345/464  P2    24/46   P3     8/16   P4     3/3    P5     4/4    P6    14/20
  P7    46/46   P8    20/20   P9    23/37   P10    0/34   P11    2/2    P12   10/12

Ablation                    auto-match   false-match   exceptions
  deterministic (L1+L2)          50.7%         0.00%          235   <- L1 only; L2 not built
  + fuzzy (L3)                      --            --           --   not built
  + LLM (L4)                        --            --           --   not built
```

**50.7%, not the ~85% the brief anticipates, and that is expected.** These datasets are
deliberately pathology-dense (`SPEC.md` §4.1): 42.9% of settlement batches have δ ≠ 0 under a
trivial join, all twelve pathologies appear at least twice in ~480 records, and Layer 1
approves **only** where the settlement identity balances at exactly zero tolerance. A join
that did not verify the arithmetic would report far more coverage and mean far less. The
headroom is Layers 2–4's to earn.

**On the 0.00% false-match rate.** The anti-hallucination protocol says to treat that as a bug
until proven otherwise, so it was investigated rather than celebrated. It is structural: every
approved group balanced at δ == 0 exactly, and a group containing a wrong member cannot
balance unless its arithmetic coincidentally sums. The detector was then mutation-tested —
corrupting an approved group makes the count rise, and every record in a wrongly-composed
group counts, per the set-equality rule. `tests/test_harness.py` holds both checks.

**Reading the per-mechanism table.** This is the diagnostic, and it already says something:
`duplicate_reference_contamination resolved 2` means pathology 2 is fully handled *at Layer 1*
by the reference-plus-value-date composite key, not deferred to Layer 2. And
`credit_without_parseable_utr ... MISSING_BANK_ROW 2` exposes a real classification gap — those
bank credits exist, they simply carry no readable reference, and an exact-match layer cannot
tell that apart from a genuine feed gap. Splitting `MISSING_BANK_ROW` from
`UNPARSEABLE_NARRATION` needs narration parsing, so it is a Phase 5 gate item.

**Reading the per-pathology row.** `P10 0/34` is honest: pathology 10 is the on-hold-release
mechanism, whose batches all have δ ≠ 0, and Layer 2 does not exist yet. `P7 46/46` and
`P8 20/20` are already correct because both are *refusals* — declining to match is the right
answer and Layer 1 declines. Counts overlap and do not sum to 477 (D-0016).

Each entry takes this form:

```
$ git rev-parse --short HEAD
<sha>
$ date -u '+%Y-%m-%d %H:%M UTC'
<timestamp>
$ make eval
<pasted stdout, verbatim, including the ablation table>
```

## Phase 3 — Layer 2 (bounded settlement decomposition)

```
$ git rev-parse --short HEAD
75d89bc
$ date -u '+%Y-%m-%d %H:%M UTC'
2026-08-26 16:46 UTC
$ make eval
Dataset: dev_seed_11  data fb32ade9   SHA: 75d89bc   2026-08-26 16:46
Records processed         477          Wall clock    0.024s
Auto-matched              319    66.9%   Throughput  20240 rec/s
  Layer 1  exact            242    50.7%
  Layer 2  netting           77    16.1%
  Layer 3  fuzzy             --       --   not built yet
  Layer 4  LLM+verified      --       --   not built yet
False matches               0    0.00%   <- precision, not coverage
Exceptions                158    33.1%    Rs 1,21,35,570.37 at risk
  correctly flagged       103    65.2%
  missed matches           55    34.8%
  by type: TIMING_OUTSIDE_WINDOW 53, AMBIGUOUS 42, MISSING_BANK_ROW 40, SUBSET_SEARCH_EXHAUSTED 17, UNCLASSIFIED 4, MISSING_GATEWAY_ROW 2
  by class: absent 57, undetermined 46
LLM calls                   0          Calls / 100  0.0%
Cost / 1000            Rs TBD          USD 0.000000 total
Audit ledger               34 entries   head c4fdb882a61f
By mechanism  (delta != 0 batches; ground-truth attribution. refused is a SUCCESS, exhausted is an honest failure)
  credit_without_parseable_utr       2 batches  resolved 0  refused 0  exhausted 0  unclassified 0  MISSING_BANK_ROW 2
  duplicate_reference_contamination  2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  export_cutoff_skew                 3 batches  resolved 3  refused 0  exhausted 0  unclassified 0
  multiple_subsets_explain_delta     2 batches  resolved 0  refused 2  exhausted 0  unclassified 0
  on_hold_release_misdated           2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  pool_beyond_node_budget            1 batch    resolved 0  refused 0  exhausted 1  unclassified 0
FINDING  UNCLASSIFIED holds 4 records (2.5% of exceptions). Target <= 13 at this phase, 0 by Phase 5.
         Every record in it has a home in the enum; the count is a measure of layers not yet built, not of records that defy classification.
By pathology  (records carry >=1, so these OVERLAP and do not sum to 477)
  P1   409/464  P2    24/46   P3    12/16   P4     3/3    P5     4/4    P6    16/20
  P7    46/46   P8    20/20   P9    37/37   P10   34/34   P11    2/2    P12   10/12

Ablation (same dataset, layers enabled cumulatively)
  arm                  auto-match   false-match   exceptions   UNCLASSIFIED
  exact only (L1)          50.7%         0.00%          235            193
  + netting (L2)           66.9%         0.00%          158              4   +16.1pp
  + fuzzy (L3)                --            --           --             --   not built
  + LLM+verified (L4)         --            --           --             --   not built
  False-match rate is reported on every arm: an arm that raises coverage while also
  raising false matches is a regression being sold as an improvement.
```

**+16.1pp, false matches flat at 0.00%.** Both numbers come from the ablation table, which
re-runs each arm on the *same* data. That re-running is not ceremony: the datasets changed at
this phase (D-0020 moved M6's hardness knob), so the Phase 2 row below measures dataset
`371df9be` while this one measures `fb32ade9`. **The two headline rates are not directly
comparable** — which is exactly what the Dataset SHA column is for, and why the ablation exists.

**All three Layer 2 outcomes are present, on both datasets.** M1 3/3 and M2 2/2 resolved, M5
2/2 **refused** with subset evidence recorded, M6 1/1 **exhausted**. A bounded search that only
ever succeeds has not demonstrated its bound; this one demonstrates all three.

**`UNCLASSIFIED` fell 193 → 4** (2.5% of exceptions, against a ≤13 ceiling for this phase). The
53 records now typed `TIMING_OUTSIDE_WINDOW` are pending writebacks whose settlement falls
outside the export period — previously they sat in `UNCLASSIFIED` saying nothing.

**`P10 34/34`, up from 0/34.** Pathology 10 is the misdated on-hold release, and every one of its
records is now resolved: the staged window had to widen past the documented T+2 cycle to reach
them, which is precisely the signal that mechanism was built to produce.

**Three bugs were found and fixed at this phase**, all in `docs/WHAT_BROKE.md`. The one worth
reading is the search double-counting solutions: it manufactured *false ambiguity*, and because
`AMBIGUOUS` scores as a success, it made the system look principled while quietly failing to
resolve resolvable batches.

---

### Run log

**Dataset SHA** is populated at eval time by `eval/provenance.py`, from the emitted files on
disk. Without it a row records a number but not *what the number is about*: a row from before
the datasets were regenerated is otherwise indistinguishable from one after, and since this
file is append-only and rows are compared across phases, that ambiguity would quietly
invalidate every comparison in it.

Read a row whose Dataset SHA differs from the rows around it as **measuring something else**.
It is not comparable to them, however similar the numbers look.

| Phase | Dataset | Dataset SHA | Auto-match | False-match | Exceptions | Git SHA | Date |
|---|---|---|---|---|---|---|---|
| 2 (baseline, exact only) | `dev_seed_11` | `371df9be` | 50.7% | 0.00% | 235 | `ab178c5` | 2026-08-26 |
| 2 (re-run on current data) | `dev_seed_11` | `fb32ade9` | 50.7% | 0.00% | 235 | `75d89bc` | 2026-08-26 |
| 3 (+ netting) | `dev_seed_11` | `fb32ade9` | 66.9% | 0.00% | 158 | `75d89bc` | 2026-08-26 |
| 4 (+ fuzzy) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD | TBD |
| 5 (+ LLM) | `dev_seed_11` | TBD | TBD | TBD | TBD | TBD | TBD |
| 6 (final, once) | `holdout_seed_97` | TBD | TBD | TBD | TBD | TBD | TBD |

`holdout_seed_97` gets exactly one row, filled in Phase 6. Whatever it prints is what
ships, even if it is worse than dev.

A row is also **not trustworthy** when its provenance reports `drift` (the files on disk
disagree with the committed manifest) or `absent` (no manifest). Both print inline in the
metrics block header, so the condition cannot be missed while reading the numbers.

### Dataset provenance, current

Not metrics — the reference point rows are measured against. Produced by
`eval/provenance.capture()`, at git `1e60254`:

```
Dataset: dev_seed_11      data 371df9be    manifest=match
Dataset: holdout_seed_97  data a3cccfd9    manifest=match
```

The digest is per dataset, so regenerating the holdout does not invalidate the provenance of
every dev row. Full per-file hashes are in `data/DATASET_HASHES.txt`.
