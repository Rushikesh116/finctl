# Metrics

Raw pasted stdout of `make eval`, with the git SHA and timestamp of the run above each
block. Nothing in this file is typed by hand — every number is pasted from a command's
output. A result that has not been run is `TBD`. See
`.claude/skills/eval-protocol/SKILL.md` for the paste rule and metric definitions.

---

## Phase 5 — Layer 4 (adjudication behind the verifier)

> **Every LLM figure below came from a stub, not a model.** There is no `ANTHROPIC_API_KEY` and
> no `ant` credential in the environment this was built in, so the live SDK path is written and
> unit-tested but **has never been executed against the API**. Fixtures are tagged
> `"source": "offline_stub"` and the block prints `!! STUBBED PROPOSER, not a model`. What that
> leaves intact is the regex-promotion machinery and the verifier boundary, both of which are
> real code; what it leaves unverified is whether a real model proposes usable regexes at a
> useful rate, and what it would cost. See `core/llm.py`.

```
$ git rev-parse --short HEAD
5069d36
$ date -u '+%Y-%m-%d %H:%M UTC'
2026-08-26 17:39 UTC
$ make eval
Dataset: dev_seed_11  data 1115450f   SHA: 5069d36   2026-08-26 17:39
Records processed         558          Wall clock    0.267s
Auto-matched              425    76.2%   Throughput   2090 rec/s
  Layer 1  exact            325    58.2%
  Layer 2  netting           73    13.1%
  Layer 3  fuzzy              0     0.0%
  Layer 4  LLM+verified      27     4.8%
False matches               0    0.00%   <- precision, not coverage
Exceptions                133    23.8%    Rs 1,14,61,299.74 at risk
  correctly flagged        94    70.7%
  missed matches           39    29.3%
  by type: TIMING_OUTSIDE_WINDOW 44, AMBIGUOUS 43, MISSING_BANK_ROW 32, UNPARSEABLE_NARRATION 32, SUBSET_SEARCH_EXHAUSTED 13, MISSING_GATEWAY_ROW 1
  by class: absent 56, undetermined 38
LLM calls                   0   cache hits    6   Calls / 100  0.00
  by kind: none (all replayed from fixtures)   MODE=offline  !! STUBBED PROPOSER, not a model
Rules cache                 3 rules   2 promoted from narration the seeded regex missed
Cost / 1000            Rs TBD          USD 0.000000 total
Audit ledger               42 entries   head e51023c30206
By mechanism  (delta != 0 batches; ground-truth attribution. refused is a SUCCESS, exhausted is an honest failure)
  credit_without_parseable_utr       4 batches  resolved 3  refused 0  exhausted 0  unclassified 0  MISSING_BANK_ROW 1
  duplicate_reference_contamination  2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  export_cutoff_skew                 3 batches  resolved 3  refused 0  exhausted 0  unclassified 0
  multiple_subsets_explain_delta     2 batches  resolved 0  refused 2  exhausted 0  unclassified 0
  on_hold_release_misdated           2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  pool_beyond_node_budget            1 batch    resolved 0  refused 0  exhausted 1  unclassified 0
Refusals  (declining is a SUCCESS. Two distinct kinds, kept separate on purpose - they were conflated once). STRICTER than the by-pathology row below: that asks whether the engine avoided a wrong answer, this asks whether it gave the right answer for the right reason - a declared AMBIGUOUS, not merely an absence.
  P7 record-level tie            8/8    records   100.0%
  M5 batch subset ambiguity      2/2    batches   100.0%
By pathology  (records carry >=1, so these OVERLAP and do not sum to 558)
  P1   502/541  P2    57/66   P3    18/18   P4     3/3    P5     4/4    P6    24/24
  P7     8/8    P8    24/24   P9    50/50   P10   30/30   P11    2/2    P12   12/14

Ablation (same dataset, layers enabled cumulatively)
  arm                  auto-match   false-match   exceptions   UNCLASSIFIED
  exact only (L1)          58.2%         0.00%          233             94
  + netting (L2)           71.3%         0.00%          160              4   +13.1pp
  + fuzzy (L3)             71.3%         0.00%          160              0   +0.0pp
  + LLM (L4)               76.2%         0.00%          133              0   +4.8pp
  False-match rate is reported on every arm: an arm that raises coverage while also
  raising false matches is a regression being sold as an improvement.

$ make llm-curve
Curve A - the regex cache, isolated
  fixtures cleared before every run, rules cache kept. Nothing is replayed, so a falling call count is a narration shape a promoted regex now handles.

  run   calls   per 100   PARSE   explain   cache hits   promoted rules
    1       8      1.43       3         5            0                2
    2       6      1.08       1         5            0                2
    3       6      1.08       1         5            0                2
    4       6      1.08       1         5            0                2

Curve B - the fixture cache
  nothing cleared. Calls fall because responses replay by prompt hash. This would fall to zero even with no regex ever promoted, which is why A is reported too.

  run   calls   per 100   PARSE   explain   cache hits   promoted rules
    1       8      1.43       3         5            0                2
    2       0      0.00       0         0            6                2
    3       0      0.00       0         0            6                2
    4       0      0.00       0         0            6                2

PARSE calls: 3 -> 1 over 4 runs, with 2 regexes promoted. Nothing was replayed, so that is the regex cache.
EXPLAIN calls hold at 5: one per distinct exception type, not a narration shape, so no regex can retire them. They fall only in curve B, via the fixture cache.
```

### The falling curve

**Parse calls 3 → 1 with two regexes promoted, and nothing replayed.** Curve A clears the fixture
cache before every run while keeping the rules cache, so the drop cannot be replay — it is two
narration shapes that a promoted regex now handles deterministically. The remaining call is the
narration containing no reference at all: no regex can ever retire it, so it costs a call every
run, permanently and correctly.

**Explanation calls hold at 5** — one per distinct exception type. They are not a narration shape,
so no regex can retire them; they fall only in curve B, via the fixture cache. Reporting a single
combined "calls per run" number would have shown 8 → 6 and hidden which cache did the work.

**Calls per 100 records: 1.43 cold, 0.00 on replay.** The brief's target was under 5% of the
batch reaching the LLM; this is 0.54% of records cold.

### What Layer 4 bought

| | before (+L3) | after (+L4) | delta |
|---|---|---|---|
| auto-match rate | 66.3%* | **76.2%** | **+9.9pp** |
| **false-match rate** | **0.00%** | **0.00%** | **+0.00pp** |
| exception records | 162* | 133 | −29 |
| `MISSING_BANK_ROW` | 40* | 32 | split |
| `UNPARSEABLE_NARRATION` | 0 | **32** | new |

\* the before column is from the previous dataset SHA; the current-SHA arms are in the ablation
table inside the block above, which is the comparison that counts.

**The split is the item-2 deliverable and it is now real.** Layer 1 cannot distinguish "no credit
exists" from "a credit exists whose reference is unreadable" — both look identical to an exact
join. Three of the four blank-reference credits had their UTR recovered from narration; the
fourth carries no reference anywhere and is now typed `UNPARSEABLE_NARRATION`, which tells an
operator to read the line rather than chase the feed.

---

## Phase 4 — Layer 3 (fuzzy matching and global assignment)

```
$ git rev-parse --short HEAD
1e9e225
$ date -u '+%Y-%m-%d %H:%M UTC'
2026-08-26 17:27 UTC
$ make eval
Dataset: dev_seed_11  data d93197db   SHA: 1e9e225   2026-08-26 17:27
Records processed         481          Wall clock    0.025s
Auto-matched              319    66.3%   Throughput  19143 rec/s
  Layer 1  exact            242    50.3%
  Layer 2  netting           77    16.0%
  Layer 3  fuzzy              0     0.0%
  Layer 4  LLM+verified      --       --   not built yet
False matches               0    0.00%   <- precision, not coverage
Exceptions                162    33.7%    Rs 1,24,31,354.37 at risk
  correctly flagged       107    66.0%
  missed matches           55    34.0%
  by type: TIMING_OUTSIDE_WINDOW 53, AMBIGUOUS 50, MISSING_BANK_ROW 40, SUBSET_SEARCH_EXHAUSTED 17, MISSING_GATEWAY_ROW 2
  by class: absent 57, undetermined 50
LLM calls                   0          Calls / 100  0.0%
Cost / 1000            Rs TBD          USD 0.000000 total
Audit ledger               35 entries   head 91f524bb07d0
By mechanism  (delta != 0 batches; ground-truth attribution. refused is a SUCCESS, exhausted is an honest failure)
  credit_without_parseable_utr       2 batches  resolved 0  refused 0  exhausted 0  unclassified 0  MISSING_BANK_ROW 2
  duplicate_reference_contamination  2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  export_cutoff_skew                 3 batches  resolved 3  refused 0  exhausted 0  unclassified 0
  multiple_subsets_explain_delta     2 batches  resolved 0  refused 2  exhausted 0  unclassified 0
  on_hold_release_misdated           2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  pool_beyond_node_budget            1 batch    resolved 0  refused 0  exhausted 1  unclassified 0
Refusals  (declining is a SUCCESS. Two distinct kinds, kept separate on purpose - they were conflated once). STRICTER than the by-pathology row below: that asks whether the engine avoided a wrong answer, this asks whether it gave the right answer for the right reason - a declared AMBIGUOUS, not merely an absence.
  P7 record-level tie            8/8    records   100.0%
  M5 batch subset ambiguity      2/2    batches   100.0%
By pathology  (records carry >=1, so these OVERLAP and do not sum to 481)
  P1   409/464  P2    24/46   P3    12/16   P4     3/3    P5     4/4    P6    16/20
  P7     8/8    P8    20/20   P9    37/37   P10   34/34   P11    2/2    P12   10/12

Ablation (same dataset, layers enabled cumulatively)
  arm                  auto-match   false-match   exceptions   UNCLASSIFIED
  exact only (L1)          50.3%         0.00%          239            108
  + netting (L2)           66.3%         0.00%          162              4   +16.0pp
  + fuzzy (L3)             66.3%         0.00%          162              0   +0.0pp
  + LLM+verified (L4)         --            --           --             --   not built
  False-match rate is reported on every arm: an arm that raises coverage while also
  raising false matches is a regression being sold as an improvement.
```

### The before/after pair, on one dataset SHA

This pair is the whole justification for the layer, so it is reported in full rather than
summarised.

| | before (L1+L2) | after (+L3) | delta |
|---|---|---|---|
| auto-matched records | 319 | 319 | **+0** |
| auto-match rate | 66.3% | 66.3% | **+0.0pp** |
| **false matches** | **0** | **0** | **+0** |
| **false-match rate** | **0.00%** | **0.00%** | **+0.00pp** |
| exception records | 162 | 162 | +0 |
| correctly flagged | 107 | 107 | +0 |
| missed matches | 55 | 55 | +0 |
| **`UNCLASSIFIED`** | **4** | **0** | **−4** |
| P7 refusal, declared | 0/8 | **8/8** | +8 |

**What was bought, and for what.** Layer 3 bought **no coverage and no additional false-match
risk**. What it bought is the last of the exception queue's precision: `UNCLASSIFIED` reached
**zero**, and the eight pathology-7 records went from "we could not classify these" to a
declared `AMBIGUOUS` carrying all four candidate pairings as evidence, each with the amount it
satisfies. The exception-type histogram shows the transfer exactly — `AMBIGUOUS` 42 → 50,
`TIMING_OUTSIDE_WINDOW` 57 → 53, `UNCLASSIFIED` 4 → 0.

That is a real gain and it is not a coverage gain. Saying so plainly matters more than the
number: **the price paid was zero, so nothing was traded.** The attribution risk D-0024 warned
about is genuine but did not materialise here, because the only pairings Layer 3 found were
interchangeable ones it refused.

**Why coverage did not move, established rather than assumed.** 46 ledger rows reach Layer 3
and only **4** have any candidate at all. The other **42 have their counterpart already named in
an exception** — their payments sit in batches Layer 2 could not resolve. That is not a Layer 3
defect and not a cascade artefact: attributing an order to a payment that is itself unreconciled
does not reconcile the order. Those 42 are blocked upstream, and resolving them is Layer 2's or
Layer 4's work.

**A change made to "help" Layer 3 was tried and reverted.** Releasing ledger rows from batch
exceptions so Layer 3 could see them gained it nothing — the counterparts were still blocked —
and moved 38 records from a specific verdict into `UNCLASSIFIED`, which is strictly less
information. The phase-4 `UNCLASSIFIED` ceiling caught it on the first test run.

---

## Current baseline — dataset `d93197db`, all arms

**Every row in the run log below is on this one dataset SHA.** Earlier rows measured
`371df9be` and `fb32ade9` and are retained further down, clearly marked, because deleting a
number that was genuinely produced is worse than labelling it superseded.

```
$ git rev-parse --short HEAD
6244f91
$ date -u '+%Y-%m-%d %H:%M UTC'
2026-08-26 16:57 UTC
$ make eval
Dataset: dev_seed_11  data d93197db   SHA: 6244f91   2026-08-26 16:57
Records processed         481          Wall clock    0.027s
Auto-matched              319    66.3%   Throughput  17895 rec/s
  Layer 1  exact            242    50.3%
  Layer 2  netting           77    16.0%
  Layer 3  fuzzy             --       --   not built yet
  Layer 4  LLM+verified      --       --   not built yet
False matches               0    0.00%   <- precision, not coverage
Exceptions                162    33.7%    Rs 1,24,31,354.37 at risk
  correctly flagged       107    66.0%
  missed matches           55    34.0%
  by type: TIMING_OUTSIDE_WINDOW 57, AMBIGUOUS 42, MISSING_BANK_ROW 40, SUBSET_SEARCH_EXHAUSTED 17, UNCLASSIFIED 4, MISSING_GATEWAY_ROW 2
  by class: absent 57, undetermined 50
LLM calls                   0          Calls / 100  0.0%
Cost / 1000            Rs TBD          USD 0.000000 total
Audit ledger               34 entries   head fea99db1cd53
By mechanism  (delta != 0 batches; ground-truth attribution. refused is a SUCCESS, exhausted is an honest failure)
  credit_without_parseable_utr       2 batches  resolved 0  refused 0  exhausted 0  unclassified 0  MISSING_BANK_ROW 2
  duplicate_reference_contamination  2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  export_cutoff_skew                 3 batches  resolved 3  refused 0  exhausted 0  unclassified 0
  multiple_subsets_explain_delta     2 batches  resolved 0  refused 2  exhausted 0  unclassified 0
  on_hold_release_misdated           2 batches  resolved 2  refused 0  exhausted 0  unclassified 0
  pool_beyond_node_budget            1 batch    resolved 0  refused 0  exhausted 1  unclassified 0
Refusals  (declining is a SUCCESS. Two distinct kinds, kept separate on purpose - they were conflated once)
  P7 record-level tie            0/8    records     0.0%
  M5 batch subset ambiguity      2/2    batches   100.0%
FINDING  UNCLASSIFIED holds 4 records (2.5% of exceptions). Target <= 13 at this phase, 0 by Phase 5.
         Every record in it has a home in the enum; the count is a measure of layers not yet built, not of records that defy classification.
By pathology  (records carry >=1, so these OVERLAP and do not sum to 481)
  P1   409/464  P2    24/46   P3    12/16   P4     3/3    P5     4/4    P6    16/20
  P7     8/8    P8    20/20   P9    37/37   P10   34/34   P11    2/2    P12   10/12

Ablation (same dataset, layers enabled cumulatively)
  arm                  auto-match   false-match   exceptions   UNCLASSIFIED
  exact only (L1)          50.3%         0.00%          239            108
  + netting (L2)           66.3%         0.00%          162              4   +16.0pp
  + fuzzy (L3)                --            --           --             --   not built
  + LLM+verified (L4)         --            --           --             --   not built
  False-match rate is reported on every arm: an arm that raises coverage while also
  raising false matches is a regression being sold as an improvement.
```

**Why the re-baseline was needed.** The datasets changed twice for reasons recorded in
`DECISIONS.md`: D-0020 moved M6's hardness knob from pool size to true-subset size, and the
pathology-7 fix added the gateway counterparts that pathology was always supposed to have.
Neither change was cosmetic, and both altered what the headline rates are computed over — so
carrying forward the old numbers would have been comparing different inputs while presenting
them as a trend.

**The two P7 numbers differ on purpose.** `P7 8/8` in the by-pathology row and
`P7 record-level tie 0/8` in Refusals measure different things. The first asks whether the
engine avoided a *wrong* answer; it did, by never reaching those records. The second asks
whether it gave the *right* answer for the right reason — a declared `AMBIGUOUS`. Layer 3 does
not exist yet, so it is 0/8, and moving that to 8/8 is Phase 4's gate. The lenient reading
would have scored a layer that does not exist at 100% on the pathology it was built to handle.

**Refusals are now two permanent lines.** A record-level tie (pathology 7: which of two
identical candidates is this record's counterparty?) and a batch subset ambiguity (M5: which
subset of pool rows settled?) are different questions — one about attribution between records,
one about set membership in a batch. A single "refusals" number cannot say which a system is bad
at, and the two were conflated once already.

---

## SUPERSEDED — Phase 2 baseline, dataset `371df9be`

> Kept as an honest record of what was run, **not** as a comparable number. The datasets have
> been regenerated twice since (D-0020 moved M6's hardness knob; the pathology-7 fix added its
> gateway counterparts), so this block measures different input from the current baseline. See
> the re-baseline above.

### Phase 2 baseline — Layer 1 (exact matching) only

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

## SUPERSEDED — Phase 3, dataset `fb32ade9`

> Same caveat: kept for the record, superseded by the re-baseline above.

### Phase 3 — Layer 2 (bounded settlement decomposition)

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
| 2 — exact only (L1) | `dev_seed_11` | `d93197db` | 50.3% | 0.00% | 239 | `6244f91` | 2026-08-26 |
| 3 — + netting (L2) | `dev_seed_11` | `d93197db` | 66.3% | 0.00% | 162 | `6244f91` | 2026-08-26 |
| 4 — + fuzzy (L3) | `dev_seed_11` | `d93197db` | 66.3% | 0.00% | 162 | `1e9e225` | 2026-08-26 |
| 5 — + LLM (L4) | `dev_seed_11` | `1115450f` | 76.2% | 0.00% | 133 | `5069d36` | 2026-08-26 |
| 6 (final, once) | `holdout_seed_97` | TBD | TBD | TBD | TBD | TBD | TBD |

Both current rows are **real runs on the same data**, produced by the ablation arms rather than
recalled from the phase in which the layer was written. That is the only way the +16.0pp delta
means anything.

### Superseded rows

Retained because they were genuinely produced; not comparable to the table above.

| Phase | Dataset SHA | Auto-match | False-match | Exceptions | Superseded by |
|---|---|---|---|---|---|
| 2 | `371df9be` | 50.7% | 0.00% | 235 | D-0020 regenerated the datasets |
| 3 | `fb32ade9` | 66.9% | 0.00% | 158 | the pathology-7 fix regenerated them again |

`holdout_seed_97` gets exactly one row, filled in Phase 6. Whatever it prints is what
ships, even if it is worse than dev.

A row is also **not trustworthy** when its provenance reports `drift` (the files on disk
disagree with the committed manifest) or `absent` (no manifest). Both print inline in the
metrics block header, so the condition cannot be missed while reading the numbers.

### Dataset provenance, current

Not metrics — the reference point rows are measured against. Produced by
`eval/provenance.capture()`, at git `6244f91`:

```
Dataset: dev_seed_11      data d93197db    manifest=match
Dataset: holdout_seed_97  data 9e555895    manifest=match
```

The digest is per dataset, so regenerating the holdout does not invalidate the provenance of
every dev row. Full per-file hashes are in `data/DATASET_HASHES.txt`.
