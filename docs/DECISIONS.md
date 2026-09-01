# Decisions

Append-only. One entry per non-obvious choice, written **before** the code, not after.
Newest at the bottom.

Format: context → decision → alternatives rejected → date.

---

## D-0001 — Python 3.13 locally *and* in the container, not 3.11

**Context.** The brief pins "Python 3.11+" for the language and `python:3.11-slim` for the
Dockerfile. This machine has no 3.11: `python3` is 3.13.14 via Homebrew, with 3.12 and 3.14
also present. Following both lines literally would mean developing on one interpreter and
shipping on another.

**Decision.** Use **3.13.14** for the local venv and target `python:3.13-slim` for the
Dockerfile in Phase 6. 3.13 satisfies "3.11+", is two years mature so every pinned wheel
exists for it, and — the actual reason — keeping dev and prod on one interpreter removes a
whole class of "works locally" bug from a project whose entire value proposition is that
its numbers are trustworthy. `BOOTSTRAP_PYTHON` is a Makefile variable, so overriding this
is one flag: `make setup BOOTSTRAP_PYTHON=python3.12`.

**Alternatives rejected.**
- *Local 3.13, container 3.11* — the literal reading of the brief, and the one skew worth
  avoiding. PEP 695 syntax, `typing` changes, and stdlib behaviour all differ between them.
- *`brew install python@3.11`* — matches the brief exactly, but adds a system-level install
  nobody asked for to get an interpreter closer to end-of-life.
- *3.14* — newest, but least wheel coverage for scipy/numpy and no upside here.

**Confirmed by the user at the Phase 0 review, 2026-08-26** (Q-013). This is a knowing
deviation from the brief's stated base image; the Phase 6 Dockerfile will carry a comment
saying so. *2026-08-26*

---

## D-0002 — Global assignment via `linear_sum_assignment`, never greedy

**Context.** Layer 3 must turn a set of scored candidate pairs into a set of matches.

**Decision.** Build a cost matrix over candidates and solve it with
`scipy.optimize.linear_sum_assignment` for a globally optimal one-to-one assignment.

**Alternatives rejected.** *Greedy best-first matching* — it starves correct pairings
(taking a locally best pair can deny two other records their only correct partner) and it
inflates the false-match rate while raising the headline match rate, which is exactly the
failure mode this project is being graded on catching. Recorded here before implementation
so the choice is defensible rather than retrofitted. *2026-08-26*

---

## D-0003 — Canonical schema stores `fee_base_paise` + `gst_paise`, never a field named `fee`

**Context.** The gateway's Payment entity documents `fee` as GST-**inclusive** with `tax`
as the GST inside it. The dashboard settlement break-up shows Tax and Fee as **separate**
deductions, implying `fee` is GST-exclusive. The readings differ by exactly the GST, and
picking wrong shifts every settlement δ by 18/118 of the fee — small enough to look like a
rounding bug, large enough to fail every balance check.

**Decision.** Do not reuse the ambiguous word. FinCtl's canonical record carries
`fee_base_paise` (GST-exclusive) and `gst_paise`, and the netting identity subtracts both.
Any ingestion of a real Payment entity **converts** in exactly one audited place:
`fee_base = fee − tax`, `gst = tax`.

**Alternatives rejected.**
- *Copy `fee` and `tax` verbatim* — carries the ambiguity into every call site.
- *Pick one reading and move on* — the canonical schema above is correct under **either**
  reading; only the ingestion adapter depends on the answer, so committing now costs
  nothing and unblocks Phase 1. The question stays open as Q-002. *2026-08-26*

---

## D-0004 — Determinism comes from the fixture cache, not from `temperature=0`

**Context.** The brief specifies "temperature 0" for the Layer 4 LLM calls. Verified
2026-08-26: on current Claude models — Opus 5, Sonnet 5, Fable 5, Opus 4.7/4.8 — the
`temperature` parameter has been **removed and returns HTTP 400**. The instruction predates
that change; it is not satisfiable as written on the default model.

**Decision.** Do not send `temperature`. Replay determinism comes from invariant 4's
fixture cache: every response is written to `fixtures/llm/` keyed by a hash of the prompt,
and **a cache miss during a replay run fails loudly rather than falling through to a live
call**. Structured output via Pydantic plus the independent verifier means a varying
response cannot corrupt the ledger regardless — at worst it produces a proposal the
verifier rejects.

**Alternatives rejected.**
- *Send `temperature=0` anyway* — a 400 on every call.
- *Downgrade to a model that still accepts `temperature`* (e.g. Haiku 4.5) — trades model
  capability for a parameter that was never the real determinism mechanism. Available if
  the user wants strict literal compliance; noted as Q-003. *2026-08-26*

---

## D-0005 — Half-up integer rounding; largest-remainder splits

**Context.** Two rounding situations exist: a percentage of an amount (GST on a fee), and
splitting a total across parts (proration).

**Decision.** Percentages use `pct_half_up(base, num, den) = (base*num + den//2) // den`,
requiring a non-negative base so floor division cannot skew a negative. Splits use the
largest-remainder method with ties broken by lowest index, and carry the post-condition
`sum(parts) == total` as a **property test over random inputs**, not three hand-picked cases.

**Alternatives rejected.**
- *Banker's rounding* — defensible statistically, but half-up is the convention a finance
  reviewer expects on Indian tax arithmetic, and a reviewer who has to ask which rounding
  you used has already stopped trusting the number.
- *`Decimal` with `ROUND_HALF_UP`* — correct, but puts a non-`int` type in the money path
  and invites a `float` round trip at the first careless call site. *2026-08-26*

---

## D-0006 — Commit the DEMO_MODE SQLite artefact, against the blanket `*.db` ignore

**Context.** `.gitignore` covers `*.db` per the brief. But `DEMO_MODE=1` is specified to
serve "a pre-computed run from committed fixtures and the SQLite file", and `make demo`
must work on a clean clone with no API key. Those two lines conflict.

**Decision.** Keep the blanket `*.db` ignore — run databases are build output — and add a
single negation for `fixtures/demo/finctl_demo.db`. The exception is narrow, commented in
`.gitignore`, and pointed here.

**Alternatives rejected.**
- *Drop the `*.db` ignore* — invites committing a dev database by accident.
- *Regenerate the demo DB at container start* — needs a seed step and an LLM call on cold
  start, which is precisely what DEMO_MODE exists to avoid. *2026-08-26*

---

## D-0007 — Dataset freeze enforced by seed plus a committed hash manifest

**Context.** The two datasets must be "frozen", but `data/generated/` is gitignored and
`make demo` regenerates them on a clean clone.

**Decision.** Do not commit the CSV/JSON. Commit **`data/DATASET_HASHES.txt`**, a SHA-256
manifest of every generated file. `make seed` writes the files; a test regenerates and
compares against the manifest. "Frozen" becomes a checkable claim instead of a promise, and
determinism of the generator gets tested as a side effect.

**Alternatives rejected.**
- *Commit the datasets* — ~1 MB of derived data in git, and it hides generator
  non-determinism instead of exposing it.
- *Trust the seed* — a refactor can change the consumption order of a PRNG without
  changing the seed, silently producing a different dataset with identical provenance. That
  is the exact bug the manifest catches. *2026-08-26*

---

## D-0008 — `debit_paise`/`credit_paise` pair, not a signed `amount`

**Context.** Every source has directional money movements.

**Decision.** Store two non-negative integers, exactly as the settlement recon report
does. Direction is which field is populated. Signs are applied when terms enter the netting
identity, never baked into a stored amount.

**Alternatives rejected.** *A single signed `amount_paise`* — one inverted sign silently
turns a refund into a receipt, and the resulting δ looks like a data problem rather than a
code problem. Mirroring the upstream shape also means the ingestion adapter has nothing to
decide. Note the knock-on: an `adjustment` can therefore be a **credit**, so the brief's
`− Σ adjustments` becomes `− Σ adjustment_debits + Σ adjustment_credits`. *2026-08-26*

---

## D-0009 — Secret scanning installed in Phase 0, not Phase 6

**Context.** The brief lists "a pre-commit check for key-shaped strings" under Deployment
(Phase 6).

**Decision.** Install it in Phase 0. `scripts/check_secrets.py` runs on the staged diff via
`.githooks/pre-commit`, wired up by `make setup`.

**Rationale.** The risk window for committing a key is Phases 1–5, so a guard that only
exists from Phase 6 guards nothing. It is ~80 lines, matches known vendor key shapes rather
than "looks like entropy" (a scanner that cries wolf gets disabled, and a disabled scanner
protects nothing), and has an explicit `pragma: allow-secret` escape hatch.

Verified, not assumed: a planted fake `sk-ant-…` and `AKIA…` were staged, the hook blocked
the commit, and `git log` confirmed nothing landed. The allowlist marker was confirmed to
release a flagged line. *2026-08-26*

---

## D-0010 — `data/scenarios.toml` via stdlib `tomllib`, not `scenarios.yaml`

**Context.** The brief asks for `data/scenarios.yaml` holding the twelve pathologies and
their mix weights. Nothing in the pinned stack parses YAML and Python has no stdlib YAML, so
honouring the filename means adding a dependency — which requires approval.

**Decision.** `data/scenarios.toml`, parsed with stdlib `tomllib` (Python 3.11+). Approved by
the user at the Phase 0 review (Q-001). No dependency added.

**Alternatives rejected.**
- *Add `pyyaml`* — keeps the exact filename, but spends the project's one "new dependency"
  budget on a config parser rather than on anything that reconciles money.
- *`scenarios.json`* — strictly stdlib and the most boring option, but JSON has no comments.
  The pathology mix is precisely the file where a judge benefits from inline explanation of
  *why* a weight is what it is, so losing comments is a real cost.

TOML keeps the comments and adds nothing. *2026-08-26*

---

## D-0011 — `SPEC.md` §4 freezes on separate `fee_base` and `gst` subtraction

**Context.** Q-002: the gateway's Payment entity documents `fee` as GST-**inclusive**, while
the dashboard settlement break-up (`Payment - Adjustment - Tax - Fee`) deducts Tax and Fee
**separately**. The brief's §1 identity also subtracts both. The readings differ by exactly
the GST.

**Decision.** Freeze the identity in the separate-subtraction form:
`− Σ fee_base − Σ gst_on_fee`. This matches both the brief and the dashboard break-up. The
generator satisfies it by construction, and the README states it as **an assumption, not a
documented contract**. Approved by the user at the Phase 0 review.

**Alternatives rejected.**
- *Freeze on a single GST-inclusive fee* — matches the Payment entity's literal wording but
  contradicts both the brief and the dashboard, and would make the reported δ disagree with
  what a merchant sees in their own dashboard.
- *Hold the freeze pending a real settlement statement* — would block Phase 1 on a fact that
  D-0003's schema already makes the generator immune to.

**What still depends on the answer:** only the adapter that ingests a real Payment entity
(`fee_base = fee − tax`, `gst = tax` under the inclusive reading). Q-002 stays open for that
one conversion, in one audited place. *2026-08-26*

---

## D-0012 — `transfer` rows are out of scope

**Context.** Q-008: the documented recon `type` enum is `payment`, `refund`, `transfer`,
`adjustment`. The twelve pathologies exercise the first, second and fourth, but never
`transfer` (Route split-payment legs) — a quarter of the real enum with no coverage.

**Decision.** Declare transfers explicitly out of scope in the README's limitations. The
generator emits no `transfer` rows, and the matcher does not pretend to handle them.
Approved by the user at the Phase 0 review.

**Alternatives rejected.** *A thirteenth pathology for transfers* — broader enum coverage,
but no fetched document defines split-payment settlement semantics, so the dataset would be
teaching the matcher a shape invented for the occasion. Reported accuracy on invented data
is worse than an acknowledged gap: the gap costs a sentence, the invention costs the
credibility of every other number. *2026-08-26*

---

## D-0013 — Frozen dataclasses for records; pydantic reserved for the boundaries

**Context.** The pinned stack lists both `dataclasses` and `pydantic` v2.

**Decision.** Internal records (`core/records.py`) are frozen `slots=True` dataclasses with
validation in `__post_init__`. Pydantic is reserved for the two places untrusted input
crosses into the system: the LLM proposal schema (Phase 5) and the API response models
(Phase 6).

**Alternatives rejected.** *Pydantic everywhere* — validation on every construction of an
object built hundreds of thousands of times per run, to guard data the generator produced
itself. The boundaries are where validation earns its cost. *2026-08-26*

---

## D-0014 — M5 records are labelled unmatchable; M6 records are labelled matchable

**Read this before changing either label. They look inconsistent and are not.**

**Context.** Two mechanisms both end in an exception, and it is tempting to treat them the
same way:

- **M5** — several distinct subsets of the pool each close δ. The generator knows which pair
  actually settled, but **the data as given does not determine it**.
- **M6** — exactly one subset closes δ, but the pool is too large to find it inside the node
  budget. The answer **is** determined; it is merely out of reach.

**Decision.** M5's records are `unmatchable` (`ambiguous_subset_undetermined`), so refusing
with `AMBIGUOUS` scores as **correctly flagged**. M6's records stay **matchable**, so
`SUBSET_SEARCH_EXHAUSTED` scores as a **missed match** — an honest failure, not a success.

**Why this must not be "simplified" into one rule.** If exhausting the budget also counted as
a correct refusal, then **making the search worse would improve the headline number.** Lower
the node budget, time out more often, and "correctly flagged" climbs while the engine
reconciles strictly less. That is a metric that pays for weakness, and it would be invisible
in the aggregate — the auto-match rate and the exception count would both look defensible.

Put the other way: **giving up and declining are not the same claim.** "I could not determine
this" is a finding the operator can act on. "I ran out of budget" is a limitation of the
implementation. Reporting them as one number hides the more interesting one and removes the
pressure to improve the search.

**Alternatives rejected.**
- *Label both unmatchable* — creates the perverse incentive above.
- *Label both matchable* — scores the pathology-7 refusal, the demo centrepiece, as a miss,
  and so penalises exactly the behaviour the brief asks for.
- *A separate "declined" third state* — the partition `auto_matched + exceptions == N` is what
  makes the metrics block checkable; a third state would need its own denominator and lose
  that.

Asserted in both directions by
`tests/test_generator.py::test_m5_is_unmatchable_but_m6_is_not`, and written up in
`SPEC.md` §4.3. *2026-08-26*

---

## D-0015 — Unmatchable reasons are classified `absent` or `undetermined`

**Context.** "This record has no match" covers two situations that need different things said
about them, and the exception queue is where the difference is felt.

**Decision.** Every reason code is registered in `core.records.REASON_CLASS` as exactly one of:

- **`absent`** — no true partner exists in the data (pathologies 8 and 11, unsettled dispute
  legs, refunds settling in a later cycle, pool distractors). The resolution is operational:
  chase the missing feed, or accept a write-off. More data would fix it.
- **`undetermined`** — a partner exists, but the data cannot identify which (pathology 7, M5).
  The resolution is a human decision or a new distinguishing key. **More data will not help**,
  because the rows are already all present and simply do not discriminate.

The class is **derived** from `reason_code` through the registry rather than stored as a
second field, so the two cannot drift apart, and an unregistered code fails loudly at `Label`
construction.

**Why it matters.** Collapsed into one bucket, the queue tells an operator to go hunting for a
bank row that is sitting right in front of them — and the two resolutions have different
costs and different owners. *2026-08-26*

---

## D-0016 — `Label.pathologies` is a sorted list, not a single `pathology`

**Amends `SPEC.md` after its Phase 1 freeze.** Recorded here as the freeze protocol requires.

**Context.** A record can exhibit several pathologies at once. A batch that settles late over
a bank holiday (pathology 9) *and* has rows stranded by the export cutoff (mechanism M1,
pathology 1) produces records that are genuinely both. The original single-valued
`pathology: int` made these compete, and the winner was whichever branch happened to be
evaluated first.

Found by spot-check, not by a test: `gw_000149` was labelled `pathology=9` when it is equally
a pathology-1 netting member.

**Decision.** `Label.pathologies: list[int]` — sorted, non-empty, no duplicates, validated in
`__post_init__`. The generator **unions** rather than overrides: every batch contributes
pathology 1 (a batch *is* a netting case by definition), a late batch adds 9, a δ mechanism
adds whatever it exhibits, and row-level properties add 3, 6 or 12.

**Why this was worth amending a frozen document.** First-past-the-post attribution does not
fail loudly, and it mis-assigns *precisely the records that are most diagnostic*. Every
pathology still cleared its floor of 2 in both datasets, so **no test failed and no gate was
missed** — the loss was invisible to the whole suite. Left in place, the first per-pathology
accuracy table in the ablation would have quietly under-reported every overlapping case, and
the natural reading of that table ("Layer 2 handles pathology 9 badly") would have been an
artefact of labelling rather than a fact about the matcher.

The cost of fixing it later is what settles it: once the harness reads these labels and
metrics are recorded against them, changing the shape invalidates every recorded number.
Cheap now, expensive after Phase 2.

**`SettlementLabel.mechanism` stays singular.** It was checked for the same defect and does
not have it: the generator's plan holds one entry per batch slot, so a batch draws exactly one
mechanism and there is nothing to drop. A list that can only ever hold one element is
speculative generality, not safety. If a batch ever draws two, that becomes a list with its
own entry here.

**Consequence for the metrics.** Per-pathology counts now **overlap and do not sum to N**.
That is stated in `eval-protocol` §4 *and printed in the metrics block itself*, because a
reader who sees per-pathology numbers exceeding the total will otherwise read it as an
inconsistency in the measurement rather than as a property of the labelling.

Guarded by `test_at_least_one_record_carries_more_than_one_pathology`, which asserts overlap
*exists* — so a future "simplification" back to an override fails instead of silently
reverting. *2026-08-26*

---

## D-0017 — audit ledger entries carry no wall-clock timestamp

**Context.** A decision log ordinarily timestamps its entries. Invariant 4 requires the same
seed and input to produce a **byte-identical** audit log.

**Decision.** Ledger entries carry a monotonic `seq` and no wall-clock field at all. Run
wall-clock lives in the metrics block, where it is a measurement rather than part of a hashed
record.

**Why.** A timestamp makes byte-identity impossible: every run differs, the hash chain cannot
be verified against a recorded one, and "deterministic and replayable" becomes an
unfalsifiable claim — the most expensive kind, because it reads as a guarantee. A sequence
number is a logical clock and is all the ordering a decision log needs; nothing downstream asks
when an entry was written, only in what order.

**Alternatives rejected.**
- *Timestamp outside the hashed payload* — the file still differs between runs, so the
  byte-comparison that proves replay still cannot be made.
- *Truncate the timestamp to the run start* — constant within a run, so it adds nothing a
  run-level field does not already carry, and reintroduces the temptation to make it finer.

Guarded by `tests/test_ledger.py::test_no_entry_carries_a_wall_clock_field`, which fails on a
well-meaning future addition, and by
`::test_two_identical_runs_produce_byte_identical_logs`. Verified across separate processes and
under `PYTHONHASHSEED=999`. *2026-08-26*

---

## D-0018 — Layer 1 approves only where the identity balances at zero tolerance

**Context.** Layer 1 could approve a group as soon as the keys join. It would score much
better: joining `settlement_utr` to `BankRow.reference` succeeds on far more batches than
balancing does.

**Decision.** A join produces a *candidate*. Layer 1 approves only after recomputing the
settlement identity and finding `δ == 0` exactly. Batches that join but do not balance are
handed to Layer 2, not approved.

**Why.** A join is not a reconciliation. The whole claim of this project is that a matched
ledger entry means the money is accounted for, and a group that has not been checked
arithmetically does not support that claim — it supports a weaker one, silently. This is also
what makes the 0.00% false-match rate structural rather than lucky: a group with a wrong member
cannot balance unless its arithmetic coincidentally sums.

The visible cost is the headline: the Phase 2 baseline is **50.7%**, not the ~85% the brief
anticipates. That is the honest number for a pathology-dense dataset under a verifying Layer 1,
and it leaves the headroom for Layers 2–4 to earn rather than borrowing it up front.
*2026-08-26*

---

## D-0019 — the node budget is the deterministic bound; the wall clock is a liveness guard

**Context.** The brief requires Layer 2's search to be bounded by "a node budget and a
wall-clock timeout, both configurable". Invariant 4 requires the same seed and input to produce
a byte-identical audit log.

**These two requirements are in tension**, and the tension is not resolvable by wishing. A
wall-clock timeout fires at a machine-dependent point, so a run that hits it produces different
solutions — and therefore a different audit log — on a faster or slower machine.

**Decision.** Both bounds exist, but they are not peers:

- **The node budget is the bound that produces results.** It is deterministic: the same input
  explores the same nodes in the same order and stops at the same place.
- **The wall clock is a liveness guard** against a pathological instance, sized so it should
  never fire. If it does, that run is not replayable, and the exception detail says which bound
  stopped it (`limit_hit`) so the audit trail never claims reproducibility it does not have.

Verified: with a 200k node budget the largest instance in either dataset finishes in ~25ms
against a 2000ms timeout, so the clock has two orders of magnitude of headroom and the node
budget always bites first. The ledger is byte-identical across processes and under
`PYTHONHASHSEED` 0, 12345 and random.

**Alternative rejected.** *Drop the wall clock entirely* — it satisfies determinism but leaves
no protection against an instance where node accounting itself is slow. A guard that should
never fire is cheap; not having one is not. *2026-08-26*

---

## D-0020 — the hardness knob for M6 is the true subset's size, not the pool's

**Context.** M6 exists so `SUBSET_SEARCH_EXHAUSTED` appears in every run. Phase 1 encoded its
hardness as a large *pool* (40–48 rows), and a Phase 1 test asserted `pool_rows_min >= 40` with
the reasoning that anything smaller would fall to meet-in-the-middle.

**That reasoning was wrong, and Phase 3 measured it.** A search that deepens by subset size
found M6's 3-row explanation among 44 candidates in ~14k nodes. M6 was *resolving*, dev had zero
exhausted cases, and the bound was going untested — the exact failure the mechanism was created
to prevent.

**Decision.** The hardness knob is `delta_rows_min/max = 12..18`: the size of the subset that
actually explains δ. Iterative deepening must exhaust every smaller size first, and sizes 1–4
over 44 candidates already cost ~150k combinations, so a 12-row answer is unreachable under any
plausible budget. Pool size stays as a secondary contributor.

**Consequence.** Both datasets were regenerated and `DATASET_HASHES.txt` updated, so the Phase 2
metrics row measures different data from the Phase 3 row. That is exactly what the Dataset SHA
column exists to make visible, and it is why the ablation table re-runs every arm on the current
data instead of quoting a remembered number.

**The general lesson, worth keeping:** for subset-sum, the search space that matters is not the
number of candidates but the depth at which the answer sits. A test asserting the wrong knob
passed happily for two phases. *2026-08-26*

---

## D-0021 — the minimal-size explanation wins, and says so

**Context.** Enumerating every subset that closes δ across all sizes is intractable at ~70
candidates. Something has to give.

**Decision.** Iterative deepening by subset size, and **the smallest size that yields any
solution wins**; larger sizes are then not searched. Ties at that size are ambiguity, not a
choice — equally-sized alternatives are equally plausible.

**Why this is a prior and not a shortcut.** The smallest set of rows that accounts for δ is the
plausible explanation; a larger set that also happens to sum to δ is a coincidence. That is how
a human reconciler reads it. Validated against ground truth on every resolvable batch in both
datasets: the minimal solution is always either uniquely the true one, or tied with alternatives
that genuinely are ambiguous. **Zero false matches resulted.**

The claim is nonetheless weaker than "exhaustively unique", so `larger_sizes_unsearched` records
when it was applied and the group's audit detail carries it. An audit trail that overstates its
own certainty is worse than one that admits a prior.

**Alternative rejected.** *Search all sizes to the cap before deciding* — sound, and
intractable: it is what made the first implementation exhaust on 6 of 8 batches while finding
nothing. *2026-08-26*

---

## D-0022 — `core/verifier.py` lands in Phase 3, not Phase 5

**Context.** The brief introduces the verifier alongside the LLM in Phase 5, described as the
module that re-checks LLM proposals. It is also described as "the only module permitted to
approve a match".

**Decision.** Build it in Phase 3 and route **both** deterministic layers through it. Layers 1
and 2 now emit `GroupProposal`; only `verifier.verify` produces a `MatchGroup`.

**Why earlier.** A verifier retrofitted after two layers already approve directly is a verifier
with holes, and the holes are exactly the paths that predate it. Routing the deterministic
layers through it first means the Phase 5 LLM path is *just another proposer* — no new trust
granted, no special case to audit, and the claim "a hallucinated match cannot enter the ledger"
becomes structural rather than a promise about one code path.

The verifier therefore trusts no proposer, including the ones that cannot lie. It recomputes the
identity from the records and ignores whatever the proposing layer calculated, so a layer that
miscalculates its own δ cannot get a group approved on the strength of its own mistake — which
is a test, not a hypothetical. *2026-08-26*

---

## D-0023 — the ambiguity margin is pre-registered at exact ties, before any Layer 3 run

**Written before Layer 3 exists and before any Phase 4 number has been seen.** The margin moves
both the match rate and the false-match rate, so a value chosen after looking at what it does to
dev is a hyperparameter fitted to the test set, whatever it is called.

**Decision. The margin is zero: refuse only on an exact tie.** A candidate pair is matched if
*anything* in the data discriminates it from the runner-up, and refused if *nothing* does.

**Why this value, argued without reference to any result:**

1. **It is the only value that requires no tuning.** Every positive margin is a point on a
   continuum with no principled stopping place; zero is the degenerate case, reachable by
   reasoning alone. A number nobody had to choose cannot have been fitted.
2. **Its semantics are exact, not statistical.** "Nothing in the evidence separates these two"
   is a checkable property. "The gap is under 0.05 of the score range" is a statement about a
   scoring function's arbitrary units.
3. **The scoring inputs are discrete, so near-ties are largely an artefact a margin would
   invent.** Amount tolerance is zero (D-0024), so amounts either agree exactly or are not
   candidates at all; what remains is date proximity in whole days and the presence or absence
   of a key. Continuous near-misses are not a natural feature of this space.
4. **Pathology 7 is constructed as an exact identity** — same amount, same day, no
   distinguishing key — so an exact-tie rule is precisely what the pathology is designed to
   trigger. It needs no slack to be caught.

**Pre-registered commitments, binding regardless of what the numbers turn out to be:**

- If the margin is ever changed, **both settings' full metrics blocks are published side by
  side** in `docs/METRICS.md`, not just the one that was kept.
- A sensitivity sweep over margins may be reported as a **diagnostic**, explicitly labelled as
  not used for selection. If the sweep shows zero performs badly, that is a **finding to
  report**, not a licence to change the value quietly.
- The holdout is never used to choose it. It is evaluated once, in Phase 6.

**Alternative rejected.** *Pick a small positive margin such as 0.05 and justify it as "5% of
the score range".* The percentage sounds principled and is not: it inherits whatever scale the
cost function happens to use, so the same 0.05 means different things after any re-weighting —
and it would need re-tuning every time the scoring changed, against the only data available.
*2026-08-26*

---

## D-0024 — the verifier's Layer 3 contract is exact amount equality, still zero tolerance

**Context.** Layers 1 and 2 are verified against a *closed system*: the batch's expected credit
must equal an observed bank credit, exactly. Layer 3 pairs merchant-ledger rows to gateway
payments, and there is no bank credit in that relation — so the obvious implementation approves
candidates on **cost**, which is not arithmetic at all. That is where false matches would enter.

**Decision. The verifier applies zero tolerance to Layer 3 as well, on a different identity:**

```
merchant.amount_paise == gateway.credit_paise      exactly
merchant.currency     == gateway.currency
merchant.issued_at_utc <= gateway.created_at_utc   (an order precedes its payment)
```

A proposal failing any of these is `VERIFIER_REJECTED` **regardless of how good its cost was**.
Cost decides *which* candidate is proposed; it never decides whether a proposal is accepted.

**The consequence that matters: `FINCTL_AMOUNT_TOLERANCE_PAISE` stays 0.** Fuzziness lives in
the date window and in key presence — never in the money. Two records whose amounts differ by
one paisa are not near-candidates, they are not candidates.

**Being precise about what is still weaker here, because it is:**

Layers 1–2 verify that a *set* of records accounts for an *observed external scalar*. Two
records cannot satisfy that by coincidence unless their arithmetic genuinely sums. Layer 3
verifies a *pairwise equality*, and two records can satisfy that while not being the true pair —
same amount, same day, different customer. So the residual risk at Layer 3 is not arithmetic
error, it is **attribution error**: right amount, wrong counterparty.

That risk is real and it is not eliminated by this contract. What the contract does is confine
it: Layer 3 cannot produce a group whose money does not add up, only one whose money adds up and
whose counterparty is wrong. **The before/after false-match rate on identical data is the
instrument for that**, which is why the ablation reports it on every arm and why Phase 4's gate
requires it before and after.

**Alternative rejected.** *Allow a small amount tolerance so near-misses become candidates.* It
would raise the match rate and move the risk from attribution into arithmetic — a group could
then be approved whose money does not balance, which is the one thing Layers 1 and 2 make
impossible. If a tolerance is ever introduced, the false-match rate must be re-reported on both
settings and the verifier's guarantee restated, because it would no longer be "the money
balances". *2026-08-26*

---

## D-0025 — LLM provider swapped from Anthropic to Google Gemini

**Deviates from the pinned stack** (`ENGINEERING_RULES.md` → "Stop and ask before" lists adding or changing
a dependency). Requested explicitly; recorded here before the adapter was written.

**Reason: API access, not capability.** Nothing about the Anthropic path was found wanting — it
was written, unit-tested, and correct against the installed SDK. It was never *executed*, because
no credential existed. The swap is an attempt to reach a reachable API, and it is worth being
precise that this is a procurement decision, not a model-quality one. No claim is made here that
Gemini is better or worse for this task; the task is JSON extraction from a short string, which
any current frontier model handles.

**Why the swap is cheap: the verifier boundary was built for exactly this.** Invariant 3 says the
LLM proposes and a deterministic verifier disposes, and D-0022 moved the verifier in at Phase 3
so that Layers 1–3 already route through it. The consequence is that **the model is a
substitutable component by construction**, not by refactoring:

* it cannot approve a match — `core/verifier.py` recomputes the arithmetic;
* it cannot cache a bad rule — `core/rules_cache.py` validates against positive *and* negative
  examples;
* it cannot make a run unreplayable — the fixture cache is keyed by prompt hash.

So swapping providers touches one class. That property is the point of the boundary, and this is
the first time it has been tested rather than asserted.

**Scope, deliberately narrow.** One adapter replaces another: `GeminiProposer` in place of
`AnthropicProposer`, behind the existing `Proposer` interface. **No multi-provider abstraction**
— no registry, no strategy pattern, no provider-agnostic config layer. A second provider is not a
requirement, and generalising for one that does not exist would cost Phase 6 and 7 time, which
is where a judge actually looks. If a third provider is ever needed, that is when the
abstraction earns itself.

**Unchanged, by constraint:** `core/verifier.py`, the promotion gate in `core/rules_cache.py`, and
the fixture cache. Verified by test rather than intent — the promotion and verifier test suites
are provider-agnostic and were not edited.

**Technical facts, from the installed SDK and fetched docs rather than memory:**

| | |
|---|---|
| Package | `google-genai` 2.20.0 |
| Client | `genai.Client()` — reads `GEMINI_API_KEY` / `GOOGLE_API_KEY` |
| Call | `client.models.generate_content(model=, contents=, config=)` |
| Structured output | `types.GenerateContentConfig(response_mime_type="application/json", response_schema=<PydanticModel>)` — the annotation accepts a `type`, so the existing Pydantic models are passed directly |
| Parsed result | `response.parsed` |
| Provenance | `response.model_version` — the version that actually served the request, which is better provenance than the string requested |
| Tokens | `response.usage_metadata.prompt_token_count` / `.candidates_token_count` |
| Model | `gemini-3.7-flash` — the documented current default, and a Flash tier is the right shape for JSON extraction from a short string |

Note the doc page for structured output describes a *different* surface
(`client.interactions.create`, `response_format`, `output_text`) which also exists in 2.20.0. The
code is written against `client.models.generate_content` because that is the path whose parameter
names were verified directly against the installed package.

**The uncomfortable part, stated rather than discovered later.** There is no `GEMINI_API_KEY`,
no `GOOGLE_API_KEY`, no ADC file and no `gcloud` in this environment either. **The swap therefore
does not achieve its stated purpose here.** The adapter is written and tested against the real
SDK surface, so it will work the moment a key exists — but fixtures still cannot be regenerated
from real responses, the `offline_stub` tag stays because it is still true, and the question "do
the model's proposed regexes pass the negative-example gate" remains **unanswered rather than
answered**. *2026-08-26*
