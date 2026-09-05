[![tests](https://github.com/Rushikesh116/finctl/actions/workflows/test.yml/badge.svg)](https://github.com/Rushikesh116/finctl/actions/workflows/test.yml)

# FinCtl

| | |
|---|---|
| **Live API + UI** | **<https://finctl.onrender.com>** — [`/healthz`](https://finctl.onrender.com/healthz) · [`/api/run`](https://finctl.onrender.com/api/run) · [`/api/exceptions`](https://finctl.onrender.com/api/exceptions) |
| **Static report** | **<https://rushikesh116.github.io/finctl/>** — the whole run inlined, no server, no script, no network requests |

Free tier, so the first request after an idle period cold-starts. The static report is the
zero-infrastructure fallback: if the service is asleep, the numbers are still readable.

[![The FinCtl report page: a worked example showing one Rs 11,36,043.36 bank credit explained by
eight rows, the headline metrics, and the four-method cascade](docs/img/report.png)](https://rushikesh116.github.io/finctl/)

## Three findings

**1.** The model reported **confidence 95** on a regex that was safe *and* on one that would have
poisoned the rules cache permanently — the gate did all the discriminating, and any design gating
on confidence would have cached both. → [AI judgment](#ai-judgment)

**2.** The promotion gate **rejected real model output**, in the exact shape a hand-written test
predicted before any API key existed. → [The gate rejecting real model output](#the-gate-rejecting-real-model-output)

**3.** **Five separate tests passed while asserting the wrong thing.** That is the dominant failure
mode of this project — more common than any bug in the reconciliation logic — and the proxy table
is the finding. → [What broke](#what-broke)

```bash
make setup
make demo     # seed + run + eval + report, from clean, with no API key set
```

`make demo` is the one command to run. It works on a fresh clone with no `.env` and no key,
because Layer 4's model responses replay from committed fixtures keyed by prompt hash. Verified by
actually cloning into an empty directory, not by testing in the working tree.

---

## What it does

FinCtl reconciles three descriptions of the same money — a merchant's order ledger, a payment
gateway's transaction records, and a bank statement — and decides which records belong together.
Payment gateways do not settle transactions one at a time; they settle in **net batches**, so a
single bank credit is the arithmetic result of many payments netted against refunds, fees, GST and
adjustments. That makes reconciling a bank line **set reconstruction against a single scalar**
rather than row-to-row matching, which is why a join on reference alone leaves roughly a quarter of
the work undone. Every proposed match — including every one a language model suggests — must pass
an independent arithmetic re-check before it can enter the ledger, and anything that fails is
declined with its evidence attached.

### Why this is not a join

A real batch from the dev dataset, `setl_000014`. The bank shows **one** credit:

```
Rs 11,36,043.36    value date 2026-03-27 IST
narration: NEFT-RAZORPAYSOFTWARE-UTR1518846479r5bne1-STL
```

Four gateway rows carry that settlement id. A trivial join finds exactly these:

```
row          type       credit          fee_base      GST       net contribution
gw_000135    payment    Rs 1,40,254.00  Rs 2,805.08   Rs 504.91   Rs 1,36,944.01
gw_000136    payment    Rs 1,80,483.00  Rs 3,609.66   Rs 649.74   Rs 1,76,223.60
gw_000137    payment    Rs 1,23,855.00  Rs 2,477.10   Rs 445.88   Rs 1,20,932.02
gw_000138    payment    Rs 1,35,903.00  Rs 2,718.06   Rs 489.25   Rs 1,32,695.69
                                                          TOTAL   Rs 5,66,795.32
```

**The join accounts for 49.9% of the credit.**

```
Rs 11,36,043.36  (bank)  −  Rs 5,66,795.32  (joined)  =  δ  Rs 5,69,248.04
```

The other half of the money is in rows whose `settlement_id` is **null**. Nothing links them to
this batch — that is the whole difficulty. They sit in an unassigned pool, and the question is
which subset of that pool sums to δ:

```
gw_000139   Rs 1,13,151.09
gw_000140   Rs 1,48,674.48      exactly one subset closes δ, and it is all four:
gw_000141   Rs 1,94,461.78      1,13,151.09 + 1,48,674.48 + 1,94,461.78 + 1,12,960.69
gw_000142   Rs 1,12,960.69                                  =  Rs 5,69,248.04
```

So the bank line is explained by **eight** rows, four of which no key connects to it. There is no
join that finds them; you have to search, bounded, and be willing to say "I don't know" when more
than one subset fits. On this dataset that search is what takes auto-matching from 58.2% to 71.3%.

Money is integer paise everywhere — never a float, never a `Decimal` round trip — and is formatted
to rupees only in the renderer. `fee_base` is GST-exclusive and `gst` is its GST, because the
gateway's own `fee` field is GST-*inclusive* and reusing that ambiguous word is how a rounding
error becomes a reconciliation error.

---

## Results

Measured on `dev_seed_11`, 558 records. Pasted from `make eval` — no number in this repository is
typed by hand.

```
Dataset: dev_seed_11  data 1115450f   SHA: 867152e   2026-09-05 16:23
Adjudicator: offline_stub / gemini-3.7-flash   !! STUBBED PROPOSER, not a model (6 responses)
  ^ counts THIS RUN's responses. 1 cached rule(s) were authored by a real model; those narrations now resolve via the promoted regex, so their fixtures are never consulted and the model does not appear above. Its contribution moved from the response cache into the rules cache, which is what promotion is for.
Records processed         558          Wall clock    0.295s
Auto-matched              425    76.2%   Throughput   1893 rec/s
  Layer 1  exact            325    58.2%
  Layer 2  netting           73    13.1%
  Layer 3  fuzzy              0     0.0%
  Layer 4  LLM+verified      27     4.8%
False matches               0    0.00%   <- precision, not coverage
Exceptions                133    23.8%    Rs 1,14,61,299.74 at risk
  correctly flagged        94    70.7%
  missed matches           39    29.3%
Value in the run    Rs 8,77,64,133.83          <- see 'value denominator' below
  value matched     Rs 6,91,46,021.36    78.8%
  value unmatched   Rs 1,86,18,112.47    21.2%   the two sum to the total, exactly
  value denominator: per record across all three sources, the same population the record rate uses.
    One sale is counted up to three times -- as a ledger row, a gateway payment and inside a bank credit --
    so this exceeds the money that moved. Per source: merchant Rs 2,66,64,589.00  gateway Rs 3,39,09,140.33  bank Rs 2,71,90,404.50
  NB 'at risk' above is Rs 1,14,61,299.74, which is NOT this figure: it sums each exception's own
    amount-at-risk (a batch's expected credit, say), not the gross value of every record it names.
  by type: TIMING_OUTSIDE_WINDOW 44, AMBIGUOUS 43, MISSING_BANK_ROW 32, UNPARSEABLE_NARRATION 32, SUBSET_SEARCH_EXHAUSTED 13, MISSING_GATEWAY_ROW 1
  by class: absent 56, undetermined 38
LLM calls                   0   cache hits    6   Calls / 100  0.00 (replay: all 6 responses from cache)
  cold calls / 100: not measured -- this run replayed from cache, so it cannot report a cold rate. The last cold attempt was terminated by provider quota exhaustion; see docs/METRICS.md.
  by kind: none (all replayed from fixtures)   MODE=replay
Rules cache                 3 rules   2 promoted from narration the seeded regex missed
  authored by: google-gemini/gemini-3.7-flash x1, offline_stub/gemini-3.7-flash x1
Adjudication             0.0s   retries 0   backoff     0s   (1% of wall clock)
Cost / 1000            Rs TBD          USD 0.000000 total
Audit ledger               42 entries   head 51b54deedc60
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
```

Each ablation arm is a **real run** on the same dataset, not a subtraction:

```
Ablation (same dataset, layers enabled cumulatively)
  arm                  auto-match   false-match   value matched   exceptions   UNCLASSIFIED
  exact only (L1)          58.2%         0.00%           57.2%          233             94
  + netting (L2)           71.3%         0.00%           73.4%          160              4   +13.1pp rec  +16.2pp val
  + fuzzy (L3)             71.3%         0.00%           73.4%          160              0   +0.0pp rec  +0.0pp val
  + LLM (L4)               76.2%         0.00%           78.8%          133              0   +4.8pp rec  +5.4pp val
  False-match rate is reported on every arm: an arm that raises coverage while also
  raising false matches is a regression being sold as an improvement.
  Records and value are reported separately because a layer can buy a lot of one and
  little of the other.
```

The false-match rate is on every arm deliberately: an arm that raises coverage while also raising
false matches is a regression being sold as an improvement.

Four things in that table are worth reading as results rather than as noise:

- **Layer 3 is the false-match containment layer, and it held at 0.00%.** It is the one place a
  wrong match could enter: it pairs merchant-ledger rows to gateway payments with *no bank credit
  in the relation*, so the obvious implementation approves on cost, which is not arithmetic. The
  before/after on the same dataset SHA — **0.00% → 0.00%** — is what the layer is for and what it
  bought. Its coverage contribution on this dataset is **0.0pp**; the layer is reported at zero
  rather than quietly removed, and the page draws its bar at the same width as the one above it.
- **Netting buys more value than records.** +13.1pp of records but **+16.2pp of value**: the batch
  reconstruction is resolving the large items, which a record-only table would hide entirely.
- **`SUBSET_SEARCH_EXHAUSTED 13`** is the bounded search visibly giving up. The track asks for a
  stopping rule; this is it firing, on one batch whose pool exceeds the node budget.
- **`missed matches 39`** — 29.3% of exceptions are records that *could* have been matched. That is
  the honest cost of a zero-tolerance verifier and margin-zero refusal.

### Date-window sweep — diagnostic only, not used for selection

`D-0023` pre-registers that a sensitivity sweep may be reported as a diagnostic and never as a
reason to retune against dev. Running it over the Layer 3 date window, shipped setting **7 days**:

```
  window     L3 resolved    false-match    TIMING_OUT    auto-match
  7 days               0          0.00%           44        76.2%
  14 days              0          0.00%           44        76.2%
  30 days              0          0.00%           44        76.2%
  60 days              0          0.00%           44        76.2%
  120 days             0          0.00%           44        76.2%
```

**Nothing moves at any setting, and the reason is more useful than the table.** The 44
`TIMING_OUTSIDE_WINDOW` exceptions are not produced by this window at all. They come from the
pending-writeback sweep: gateway rows carrying no settlement assignment that *no in-period batch
needed*, because their settlement falls outside the period the export covers. No date window can
resolve them — the counterpart is not in the file. Widening the window to 120 days changes nothing
because Layer 3 also resolves nothing at any width on this dataset.

Two findings for the limitations section rather than a licence to tune: the window is **inert on
this data**, so its shipped value of 7 is untested by evidence rather than justified by it; and
`TIMING_OUTSIDE_WINDOW` would be better named for what it is, which is *settles in a later export
period*. **The shipped setting is unchanged.**

---

## Architecture

Three sources, four layers, each handing on only what it could not resolve.

| Layer | Job | Resolved |
|---|---|---|
| 1 `core/identity.py` | Exact match on bank reference, payment id, order id | 325 |
| 2 `core/settlement.py` | Check the batch identity; if δ ≠ 0, **bounded** subset search for δ | 73 |
| 3 `core/assignment.py` | Candidate generation, then `linear_sum_assignment` for a globally optimal one-to-one assignment | 0 |
| 4 `core/adjudicate.py` | Model on the residue: parse narration, split reason codes, draft explanations | 27 |

**The verifier boundary is the load-bearing design decision.** `core/verifier.py` is the only
module that can turn a proposal into a match. Every layer — including Layer 4 — emits a
`GroupProposal`, and the verifier independently recomputes the settlement identity at **zero
tolerance** before approving. Two consequences follow, and both are structural rather than
promised:

- A hallucinated match cannot enter the ledger. It fails arithmetic the model does not perform.
- Prompt injection through bank narration cannot cause a false match. Narration is untrusted
  third-party text; the worst an injected instruction achieves is a proposal the verifier rejects.
  Every reference a model extracts is additionally checked against the set of real settlement UTRs
  before use, so an invented reference is inert.

Layer 3 is where false matches would enter if anywhere, because it pairs ledger rows to gateway
payments with no bank credit in the relation — so the obvious implementation approves on *cost*,
which is not arithmetic at all. It does not:

> **Arithmetic is still zero-tolerance** (D-0024). A candidate must satisfy exact amount equality,
> exact currency, and an order preceding its payment. `core/verifier.py` re-checks all three.
> **Cost decides which candidate is proposed; it never decides whether a proposal is accepted.**
> So Layer 3 cannot produce a group whose money does not add up — only one whose money adds up and
> whose counterparty is wrong. That residual attribution risk is real, and the before/after
> false-match rate is the instrument for it.

Measured before and after Layer 3 on the same dataset SHA: **0.00% → 0.00%**.

**Refusal is a feature.** A system that confidently matches an ambiguous pair has excellent
coverage and terrible precision. The ambiguity margin is **zero** — refuse on an exact tie —
pre-registered in `DECISIONS.md` before the layer existed, specifically so it could not be tuned
against dev results. Ambiguity is detected by *necessity*: forbid an assigned pair, re-solve, and
if the optimum is unchanged that pair was never determined. Every refusal carries its evidence:
`AMBIGUOUS` records all four pairings of a 2×2 tie, and an M5 batch refusal records the subsets
that each close δ, with a truncation flag when there are more than five.

Every decision lands in a hash-chained append-only audit ledger with **no wall-clock field**, so
the same seed and inputs produce a byte-identical log and a run is replayable.

---

## AI judgment

> **The fixture set is MIXED real and stub, and every run says so on the same line as its
> numbers.** The metrics block above carries the harness's own banner verbatim —
> `!! STUBBED PROPOSER, not a model (6 responses)` — and it is reproduced here rather than
> summarised, because a caveat that shrinks on its way to the section that discusses it is not
> a caveat.
>
> Two narration shapes were served **live** by `gemini-3.7-flash` in Phase 5, before the
> free-tier allowance of 20 requests per day was exhausted. The rest of `fixtures/llm/` was
> produced by `OfflineProposer`, which is a heuristic test double and **not a model**.
>
> The banner counts *this run's responses*, so it reads `STUBBED` even though a real model's
> work is still doing the extracting: once a proposal is promoted, the narration resolves
> through the cached regex and the fixture that produced it is never read again. The model
> correctly disappears from the response counts while its rule keeps working — its
> contribution moved from the response cache into the rules cache, which is what promotion is
> for. The harness prints that explanation adjacent to the banner rather than in a footnote.
>
> **Do not read any LLM figure in this repository as fully model-derived.**

**Calls per 100 records: 0.00 on replay**, all 6 responses from cache. The cold rate prints
`not measured` rather than an estimate, because the cold run was terminated by quota exhaustion.

The model's job is deliberately narrow: it **writes rules, it does not participate in every run**.
Asked about a bank narration shape no regex handles, it returns a candidate regex, which is
validated and cached — so that shape costs nothing from then on. The call curve falls as the cache
fills, which is the point.

Promotion is validated, not trusted. A proposed regex must compile, expose exactly one capture
group, capture exactly the expected reference, and match **none** of the negative examples.

### The gate rejecting real model output

| Narration | Source | Proposed regex | Verdict |
|---|---|---|---|
| `NEFT-RAZORPAYSOFTWARE-UTR…-STL` | seeded rule | — | resolved, **no call needed** |
| `IMPS/1888481283mjoasu/RAZORPAY SOFTWARE` | **real model** | `^IMPS/([a-zA-Z0-9]+)/` | **REJECTED** |
| `RTGS CR REF 1552002271luumnm RAZORPAY` | **real model** | `^RTGS CR REF ([A-Za-z0-9]+) RAZORPAY$` | **ACCEPTED** |
| `NEFT CR-RAZORPAY SOFTWARE-SETTLEMENT` | — | — | **not reached — quota exhausted** |

The rejection is the valuable row. The model extracted `1888481283mjoasu` correctly and proposed a
regex that is correct *for its own example*. The gate refused it:

```
pattern also matches a narration with no reference
('IMPS/SETTLEMENT/CR' -> 'SETTLEMENT'); a rule this broad would
 invent references on every unparsed credit
```

Had it been cached, every future unparsed credit reading `IMPS/SETTLEMENT/CR` would have had
`SETTLEMENT` attached as its settlement reference — permanently, silently, and to every batch. The
failing shape was predicted by a hand-written test before any API key existed, and the model
produced exactly that shape.

The acceptance is readable and safe: `^RTGS CR REF ([A-Za-z0-9]+) RAZORPAY$` is anchored on **both**
sides, `$` included, so no reference-free narration can match it. It is now in the rules cache
stamped with its author, because a promoted rule outlives the response that produced it.

**The finding underneath both rows: the model reported confidence 95 on each.** Identical
confidence for a pattern that is safe and one that would have poisoned the cache. Confidence
carried no signal about the outcome — the gate did all the discriminating. Any design gating on
`confidence >= 90` would have cached both.

### Where using the model would be a regression

`O-002` in `docs/OPEN_QUESTIONS.md`, logged as deliberately not built:

> Asking the model which of two interchangeable candidates is correct. **Not built, and it should
> not be:** pathology 7 is constructed so that *nothing in the data* discriminates. A model asked
> to choose would produce a confident answer from no evidence, which is precisely the failure the
> refusal exists to avoid. The verifier could not catch it either, because both candidates satisfy
> the arithmetic. This is the one place where adding the model would make the system worse.

Three more capabilities are logged the same way rather than built: learned amount tolerance
(`O-003`) would move false-match risk from attribution into arithmetic; regex promotion for
merchant references (`O-004`) would be capability with no test to justify it; per-exception
explanations (`O-005`) would multiply calls tenfold for text that is largely identical.

---

## What broke

The dominant failure mode of this project was not a bug in the reconciliation logic. It was
**a test that passes while asserting the wrong thing** — five separate instances, which is more
than any other category. The pattern is the finding, so it is grouped rather than scattered.

**The shape.** A check is written, it passes, and the passing is taken as evidence. But the check
tests a *proxy* for the property, the proxy and the property come apart later, and nothing fails.
Confidence accrues that was never earned, and it accrues specifically in the area the check was
supposed to protect. An absent check leaves a known gap; a wrong check closes it on paper.

Every instance sits where the property is awkward to state directly, so a proxy is inviting:

| The property | What was actually asserted |
|---|---|
| the bounded search is hard | *pool size* |
| no record is lost or double-counted | a *sum* |
| the holdout is evaluated once | *a sentence in three documents* |
| pathology 7 is refused | *a ground-truth label* |
| the deployed image reports its provenance | *an environment configured to make the assertion pass* |

**1. M6's hardness proxied by pool size.** `assert pool_rows_min >= 40`, justified by reasoning
about meet-in-the-middle. It passed for two phases while asserting nothing useful: for subset-sum
the cost is the *depth* the answer sits at, not the candidate count. A 3-row answer among 44
candidates is ~14k nodes, so the hard case resolved easily and `SUBSET_SEARCH_EXHAUSTED` never
appeared. The bounded search — the thing the track specifically asks for — shipped two phases
without ever being seen to stop. The test now computes the combinatorial cost of reaching the true
subset and asserts it exceeds 10M.

**2. The partition invariant proxied by a sum.** `auto_matched + exceptions == N`. A record in
**both** places is counted twice, and if another is simultaneously lost the errors cancel exactly —
the total reconciles over a set that is wrong in two directions at once. The invariant could not
see the class of error it existed to catch. Found by accident, by a mutation test written to prove
the *false-match detector* could fire; it has since caught a real bug in Layer 2. Disjointness is
now checked independently, because one expression cannot carry both properties.

**3. The holdout rule proxied by documentation.** `SPEC.md`, the eval protocol and `docs/ENGINEERING_RULES.md` all
stated that the holdout is evaluated once, in Phase 6. The Phase 0 Makefile passed `--holdout` on
every `make eval`, inert for two phases because no harness existed to honour it. The first real
`make eval` evaluated the holdout. Three documents agreeing is not enforcement — the rule lived in
prose and prose cannot run. `make eval` can no longer reach the holdout. **The one observation is
disclosed rather than deleted**, in Limitations below.

**4. Pathology 7 proxied by its label.** `P7 46/46` looked like the centrepiece working. It was
measuring 14 perfectly matchable M5 batch rows, because Phase 1 mapped mechanism M5 to pathology 7
— the spec says the two share a *principle*, and that became a shared *label*, so the population
under measurement was mostly not the pathology. Underneath, the pathology's own data could not
exercise it: the twins had **zero** same-amount gateway payments, so they were *unmatched*, not
*ambiguous*, and no correct engine could have produced `AMBIGUOUS` against that data. And scoring a
refusal as "did not match it" gives full marks for never reaching the record — `P7 8/8` became
`0/8` once the metric required a *declared* `AMBIGUOUS`. A layer that did not yet exist was scoring
100% on the pathology it was built to handle.

**5. The deployed environment proxied by a convenient one.** The Dockerfile bakes
`ARG GIT_SHA=unknown` into `FINCTL_GIT_SHA`, so on any platform that builds the image itself that
default is always present — and the provenance fallback returned it, never reaching
`RENDER_GIT_COMMIT` one entry later. The live service reported `git_sha: unknown`, exactly the gap
the fallback was written to close. The test passed because it set the variable **blank**, a state no
deployment produces. Two further reproduction attempts also missed, because those images were built
*with* `--build-arg` and so had a real SHA baked. What caught it was the live URL. The suite was
green at 274 tests.

### The four habits that came out of it

For each gate the question is not "does a test pass" but **"could this test pass while the property
is false?"**

1. **Mutation-test the check.** A guard never seen to fail has not been shown to work. Every guard
   added since is verified by breaking the thing it guards.
2. **Assert the property, not a stand-in.** Where the property is combinatorial, compute it.
3. **Prefer the strict reading of any metric.** Where "correct" could mean *avoided a wrong answer*
   or *gave the right answer for the right reason*, report the second. The first flatters exactly
   the components that do not exist yet.
4. **Reproduce in the environment that has the bug, not the one that is easy to build.** If a check
   needs environment variables or build flags set, those settings are part of the claim.

Other failures are logged as they happened in `docs/WHAT_BROKE.md`, with the metric on both sides:
a subset search that double-counted solutions and **manufactured false ambiguity** (which scores as
success); a record that ended up both matched and excepted because a single pass let iteration
order decide; a change made to give Layer 3 work that took `UNCLASSIFIED` from 4 to 42; a `.env`
that was never loaded, leaving a working API key invisible for five phases; and a blind retry on
`503` that converted a capacity problem into a quota problem and spent a 20-per-day allowance.

---

## Limitations

Stated plainly, because a submission graded on measured accuracy should be equally precise about
what the measurement does not cover.

**The data is synthetic, and it satisfies my own model of the domain.** This is the central
limitation and it bounds every number above. The generator and the matcher were written by the same
person from the same reading of the gateway's documentation, so a misreading shared by both is
invisible to the harness — the engine would score well on data that is wrong in the same way it is.
The metrics measure *whether the engine solves the problem as specified*, not whether the
specification matches production.

**Pathologies 10, 11 and 12 rest on assumptions the documentation does not confirm.** Each is
generated under a stated guess, tracked as an open question:

- **10, on-hold release** (`Q-006`) — the docs say a settlement can be held and that you contact
  support to release it. They do not say what happens to the held balance, when it releases, or
  whether it reappears in a later settlement. FinCtl assumes release into a later batch with a
  `+ Σ on_hold_released` term in the identity.
- **11, adjustment with no reference** (`Q-007`) — the docs define adjustments as "adjustments to
  transactions, if any" and are silent on whether a reference is required. If every real adjustment
  carries one, this pathology is fiction and `UNEXPLAINED_ADJ` measures nothing.
- **12, FX conversion** (`Q-010`) — confirmed that international cycles differ and that minor-unit
  scales vary. *Not* documented: where the rate lives, whether a spread is disclosed separately,
  and which side rounds. FinCtl stores an integer `fx_rate_micros` and converts once.

**The minimality prior is validated on this generator's output, not proven.** Layer 2 searches by
iterative deepening and the smallest subset size that yields any solution wins; larger sizes are
then not searched. The justification is that the smallest set of rows accounting for δ is the
plausible explanation and a larger coincidental sum is not — which is how a human reconciler reads
it, and it held on every resolvable batch in both datasets with zero false matches. It remains
weaker than "exhaustively unique", so `larger_sizes_unsearched` is recorded in the audit detail
whenever it was applied.

**The promotion gate is only as strong as its negative examples** (`Q-015`). Mapping where its line
falls turned up a pattern that passes and probably should not:

Reproduced byte-for-byte from `Q-015` in `docs/OPEN_QUESTIONS.md`:

```
ACCEPT  IMPS/([A-Za-z0-9]{8,40})/RAZ     both-side anchored
REJECT  IMPS/([A-Za-z0-9]{8,40})/        matches IMPS/SETTLEMENT/CR -> "SETTLEMENT"
ACCEPT  ([A-Za-z0-9]{12,})               <- passes only because no negative example
                                            happens to contain a 12+ character run
REJECT  ([A-Za-z0-9]{8,})                matches SETTLEMENT
REJECT  (\S+)                            captures too much from its own example
```

`([A-Za-z0-9]{12,})` would extract the first long alphanumeric token from *any* narration. It
clears the gate because `NEGATIVE_EXAMPLES` is five hand-written strings and none contains a run
that long — a real statement carrying an account number, an IFSC code with a suffix, or a packed
timestamp would defeat it. The gate is a filter against the negative examples it was given, not
against breadth in general, and that is a meaningfully weaker guarantee than "a bad rule cannot be
cached".

**The holdout was observed once before Phase 6, and that observation is disclosed.** The Makefile
defect described above evaluated it at commit `a2687b1`: **auto-match 50.8%, false matches 0.00%,
exceptions 236 of 480.** Nothing was tuned in response and nothing will be — the figure sits within
0.1pp of dev, so it carries no signal worth acting on even if I were willing to use it. It is
recorded here because a holdout observation that goes unmentioned is worse than one that is
disclosed.

**One narration shape was never served by a real model.** `NEFT CR-RAZORPAY SOFTWARE-SETTLEMENT`
needs one live call, and the free-tier allowance of 20 requests per day was exhausted before it was
reached. It is handled by the offline stub.

**The fixtures are mixed real and stub, and every run says so.** Stated in full under
[AI judgment](#ai-judgment), where the banner is reproduced verbatim rather than summarised.
In short: two narration shapes were served live by `gemini-3.7-flash`, the rest of the fixture
set is stub-generated, and no LLM figure here should be read as fully model-derived. The
provider was swapped from Anthropic to Google mid-project for **API access, not capability**;
the verifier boundary is what made that a one-class change.

**Not measured at all:** behaviour on real gateway data, on volumes beyond 558 records, on
multi-day settlement cycles that straddle a month boundary, or under concurrent writes. Throughput
is 2090 rec/s locally and 126 rec/s on the deployed free tier's shared CPU.

---

## Repository map

```
core/       the matcher. May not import data/ or eval/ — enforced by an import test
data/       generator, scenario config, ground-truth labels
audit/      hash-chained decision log
eval/       harness, metrics, ablation, provenance
api/        FastAPI: JSON API and the page from one process
web/        page skeleton and stylesheet, inlined at render time
fixtures/   llm/ = recorded responses by prompt hash; rules_cache.json = promoted regexes
docs/       SPEC (frozen), DECISIONS, WHAT_BROKE, METRICS, OPEN_QUESTIONS, PROGRESS
```

`core/` imports neither `data/` nor `eval/`. That one-way arrow is what makes "the matcher never
reads ground truth" mechanically checkable rather than a promise: `tests/test_invariants.py` parses
the import graph and fails on violation. 332 passed, 1 skipped.

See `docs/SPEC.md` for the frozen specification and its amendment log, `docs/DECISIONS.md` for the
25 recorded decisions with the alternatives rejected, and `docs/METRICS.md` for every metrics block
this project has produced, each with the command, git SHA and dataset SHA that produced it.
