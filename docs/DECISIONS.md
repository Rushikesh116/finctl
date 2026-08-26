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
