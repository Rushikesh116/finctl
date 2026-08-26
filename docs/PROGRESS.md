# Progress

Phase checklist with acceptance criteria and pass/fail state. Updated at the end of every
phase. This file is authoritative on "which phase are we in" — if `CLAUDE.md` disagrees,
this file wins.

> ## Next action
>
> **Phase 4 — fuzzy matching and global assignment.**
>
> Layer 2 leaves **158 exceptions**, of which the tractable targets are: 46 merchant/gateway
> rows needing candidate generation with amount tolerance and date windows, and pathology 7's
> 4 records that must come out `AMBIGUOUS`. Build order: candidate generation → cost matrix →
> `scipy.optimize.linear_sum_assignment` → the ambiguity margin.
>
> Two hard requirements: **never greedy** (D-0002 — greedy starves correct pairings and
> inflates false matches), and the false-match rate must be reported before *and* after, on the
> same data. `UNCLASSIFIED` ceiling drops to **9** and activates automatically when
> `harness.PHASE` becomes 4.
>
> `docs/SPEC.md` is **frozen**. Changing it needs a `DECISIONS.md` entry and approval.
>
> Outstanding questions, none blocking: **Q-004** (USD→INR rate; the harness prints `Rs TBD`
> until one is pinned), **Q-002/Q-005/Q-006/Q-007/Q-010/Q-011/Q-014** (domain facts no fetched
> document answers — carried as stated assumptions the README must name), **Q-009** (plugin
> installs need a human; the marketplace here is `claude-plugins-official`, so the brief's
> names will not resolve as written).

---

## Phase 0 — repository and memory scaffold · **PASS** (2026-08-26)

**Gate:** all files exist; `make setup` and `make test` run clean; `git log` shows one
commit.

| Criterion | State | Evidence |
|---|---|---|
| Repo, venv, `.gitignore` | PASS | `git init -b main`; `.venv` on Python 3.13.14 |
| Stack pinned with **verified** versions | PASS | `requirements.txt` from `pip show` after install; `requirements.lock.txt` holds the transitive closure |
| Three local skills authored | PASS | `razorpay-domain`, `money-invariants`, `eval-protocol` |
| `razorpay-domain` from fetched docs only | PASS | 9 pages fetched 2026-08-26, quoted with URLs; 8 gaps marked `UNVERIFIED` and mirrored to `OPEN_QUESTIONS.md` |
| Memory files written | PASS | `CLAUDE.md`, `SPEC.md`, `DECISIONS.md`, `PROGRESS.md`, `OPEN_QUESTIONS.md`, `METRICS.md`, `WHAT_BROKE.md`, `README.md` |
| Makefile: 8 targets + `clean` | PASS | `make help`; a test asserts every target exists **and** is documented in `CLAUDE.md` |
| `make setup` runs clean | PASS | `setup ok -- Python 3.13.14` |
| `make test` runs clean | PASS | `32 passed, 2 skipped` |
| `git log` shows one commit | PASS | single Phase 0 commit |

**Beyond the gate, done early and deliberately:**

- Invariant 1 and 2 guards are live in `tests/test_invariants.py` from day one. The two
  `float` tests **skip** until `core/money.py` exists — clearing that skip is a Phase 1
  acceptance criterion, so the guard cannot be forgotten.
- Secret scanning runs on every commit already (D-0009), because the risk window is
  Phases 1–5, not Phase 6.

---

## Phase 1 — money and the data generator · **PASS** (2026-08-26)

The most important phase: if the generator is wrong, every metric downstream is a lie.

**Gate evidence.** `112 passed, 0 skipped`. Both datasets generated in four separate `make
seed` processes under `PYTHONHASHSEED` 0, 12345, and `random` twice — all eight files
byte-identical by `cmp`, not merely hash-equal. Verified the check has power with a negative
control: injecting a `set`-iteration order leak into the merchant emission made the hashes
diverge per seed (`0dbf72be…` vs `ae09a8df…`), and removing it restored `052824af…`.

**Amended after the gate** (D-0016): `Label.pathology` became `pathologies: list[int]`. The
manifest guard caught the change immediately and identified it precisely — only the two
labels files moved, all six record files byte-unchanged — so the amendment is a relabelling,
not a perturbation of the data. Determinism re-verified across all four processes afterwards.
41% of records now carry more than one pathology; `Σ per-pathology counts` is 704 against
`N = 477`, overlapping by design.

| Measure | dev_seed_11 | holdout_seed_97 |
|---|---|---|
| Records | 477 | 480 |
| Batches / bank credits | 30 / 28 | 30 / 28 |
| **δ ≠ 0 (floor 30%)** | **12/28 = 42.9%** | **12/28 = 42.9%** |
| δ > 0 / δ < 0 | 10 / 2 | 10 / 2 |
| M5 closing subsets per case | 10, 21 (both truncate cap 5) | 6, 21 (both truncate) |
| M6 pool size | 44 | 44 |
| All twelve pathologies ≥ 2 | yes | yes |

- [x] `core/money.py` — integer minor units, parsing, Indian-grouped formatting,
      `pct_half_up`, `split_with_remainder`, integer FX conversion. **All guards `raise`,
      verified to survive `python -O`.** 64 tests pass, 0 skipped
- [x] `core/records.py` — canonical schemas per `SPEC.md` §3 (`core` owns them; `data` and
      `eval` import *from* `core`)
- [x] `data/scenarios.toml` — twelve pathologies with mix weights **plus six `[mechanism.*]`
      tables** that make δ ≠ 0 happen, stdlib `tomllib` (D-0010). 10 config tests live now
- [x] `data/generator.py` — three sources **plus** a separate ground-truth labels file
- [x] `dev_seed_11` (~500 records) and `holdout_seed_97` (~500, different seed)
- [x] `data/DATASET_HASHES.txt` — committed SHA-256 manifest (D-0007)

**Gate:**
- [x] `make seed` twice yields identical file hashes
- [x] A test asserts every pathology appears **at least twice** in **each** dataset
- [x] Both forms of the netting identity (`SPEC.md` §4) agree on every generated batch
- [x] **≥30% of settlement batches have δ ≠ 0 under the trivial `settlement_utr` join**
      (`SPEC.md` §4.1) — the test that keeps Layer 2 from being dead code
- [x] δ occurs in **both** directions: short rows (M1/M2) and over-collected rows (M4)
- [x] **Every mechanism meets its `min_instances` floor in BOTH datasets** (M1:3, M2:2,
      M3:2, M4:2, M5:2, M6:1 — 12 over 30 batches = 40%), constructed first and then filled
      by weight. A fraction on dev says nothing about the holdout, and Phase 6 gets one shot
- [x] ≥1 M5 case per dataset with **more closing subsets than the evidence cap**, so the
      truncation path of `SPEC.md` §4.2 is exercised rather than assumed
- [x] Every subset in `SettlementLabel.explaining_subsets` independently sums to δ
- [x] Pathology 8 rows are `unmatchable` with a `reason_code` and **no group** (`SPEC.md` §3.8)
- [x] Dispute legs carry `dispute_id`; pathology 11 rows carry three nulls (`SPEC.md` §5.1)
- [x] Every record has exactly one label; no record labelled twice
- [x] `split_with_remainder` has a **property test**: `sum(parts) == total` over random inputs
- [x] `test_no_float_in_money_signatures`, `test_money_module_never_calls_float` and
      `test_money_module_uses_exceptions_not_asserts` **no longer skip**
- [x] `docs/SPEC.md` frozen

---

## Phase 2 — normalisation, Layer 1, audit ledger, harness v1 · **PASS** (2026-08-26)

**Gate evidence.** `173 passed, 0 skipped`. Baseline pasted verbatim into `docs/METRICS.md`
with its command, git SHA `a2687b1` and dataset SHA `371df9be`.

| Measure | dev_seed_11 |
|---|---|
| Records | 477 |
| **Auto-matched (Layer 1 only)** | **242 — 50.7%** |
| False matches | **0 — 0.00%** |
| Exceptions | 235 — 49.3%, Rs 29,56,629.16 at risk |
| correctly flagged / missed | 116 / 119 |
| by class | absent 70, undetermined 46 |
| Audit ledger | 33 entries, chain verified, byte-identical across processes |

**50.7%, not ~85%, and that is the honest number.** The datasets are deliberately
pathology-dense and Layer 1 approves only where the identity balances at zero tolerance
(D-0018). A join that skipped the arithmetic would report more coverage and mean less.

**The 0.00% false-match rate was investigated, not celebrated**, per anti-hallucination
protocol item 7. It is structural — every approved group balanced at δ == 0 exactly — and the
detector was mutation-tested to prove it can fire.

Two defects found and logged in `docs/WHAT_BROKE.md`: `make eval` was evaluating the holdout
on every run (Phase 0 Makefile defect, now split into `make eval-holdout`, and the one
observation disclosed), and the partition invariant had a self-cancelling blind spot that a
mutation test exposed.

- [x] `core/normalize.py` — schema mapping, the UTC/IST asymmetry (`SPEC.md` §3.4), currency
      normalisation. Both failure directions of the interval rule are tested
- [x] `core/identity.py` — Layer 1 exact matching, approving **only** at zero tolerance
      (D-0018). Refuses on an ambiguous reference; treats absence as absence, not a search
- [x] `audit/ledger.py` — append-only, **hash-chained**, no wall-clock so replay is
      byte-comparable (D-0017). Tampering, removal and reordering are all detected
- [x] `eval/harness.py` — full metrics block with provenance, per-pathology overlap caveat,
      and the ablation table
- [x] `cli.py reconcile` — `make run` writes the ledger to SQLite for inspection
- [x] `eval/provenance.py` — git SHA + **dataset SHA** + timestamp per run, with `drift` and
      `absent` surfaced inline in the header. Landed early because the harness must emit it
      from its very first run, or the baseline row is unattributable

**Gate:**
- [x] `make eval` prints real numbers with only exact matching enabled — **50.7%**, not the
      anticipated ~85%, for the reasons recorded above and in D-0018
- [x] `auto_matched + exception_records == N` **raises**, and disjointness is checked
      separately after a mutation test found the sum alone self-cancelling
- [x] The metrics block header carries the dataset SHA and prints `!! DRIFT` on disagreement
- [x] Per-pathology counts print with their overlap caveat on the header line (D-0016)
- [x] Baseline pasted into `docs/METRICS.md` with command, git SHA and dataset SHA
- [x] Audit chain verified on every run; two runs byte-identical, including under a different
      `PYTHONHASHSEED`

---

## Phase 3 — settlement decomposition · **PASS** (2026-08-26)

**Gate evidence.** `215 passed, 0 skipped`. Ablation on identical data: **50.7% → 66.9%,
+16.1pp**, false matches **0.00% on both arms**, `UNCLASSIFIED` **193 → 4**.

| Mechanism | Batches | Outcome |
|---|---|---|
| M1 export cutoff skew | 3 | **resolved 3** |
| M2 on-hold release, misdated | 2 | **resolved 2** — the staged window had to widen past T+2, which is the signal |
| M4 duplicate reference | 2 | resolved 2, at Layer 1 |
| M5 multiple subsets | 2 | **refused 2** — `AMBIGUOUS`, evidence recorded, truncation visible |
| M6 beyond node budget | 1 | **exhausted 1** — `SUBSET_SEARCH_EXHAUSTED` |
| M3 unparseable UTR | 2 | `MISSING_BANK_ROW` — Layer 4's job (over-broad, see Phase 5) |

Ledger byte-identical across processes and under `PYTHONHASHSEED` 0 / 12345 / random.

The hardest algorithmic piece.

- [ ] Balance-identity check first; `δ == 0` reconciles the whole batch at once
- [ ] Bounded subset search for δ: node budget **and** wall-clock timeout, both configurable
- [ ] Overflow becomes a typed exception (`SUBSET_SEARCH_EXHAUSTED`), never a silent drop
- [ ] **≥2 distinct closing subsets ⇒ refuse with `AMBIGUOUS`**, carrying the §4.2 evidence
- [ ] **`UNCLASSIFIED` ≤ 13 records** (from 193). Layer 2 must absorb 180: M1 43, M2 34,
      M5 42 → `AMBIGUOUS`, M6 20 → `SUBSET_SEARCH_EXHAUSTED`, pool distractors 41 →
      `TIMING_OUTSIDE_WINDOW`. Enforced by a ceiling test that activates when `harness.PHASE`
      becomes 3
- [ ] **Per-mechanism outcomes in the metrics block**, not only in tests: resolved / refused /
      exhausted / unclassified per δ mechanism, on both datasets
- [ ] Every subset an `AMBIGUOUS` exception records independently sums to δ (§4.2 rule 1)
- [ ] Truncation is visible: true `subsets_found` plus a `truncated` flag, never a silent cap
- [ ] The engine finds **all** closing subsets before refusing — finding 2 of 21 and refusing
      is right by accident; ground truth's `explaining_subsets` is what makes that checkable
- [ ] Absence is not searched: a batch with a UTR and no credit is `MISSING_BANK_ROW`
      immediately (`SPEC.md` §4.1 M0)

**Gate:**
- [ ] Match rate improves **measurably** over Phase 2
- [ ] False-match rate does **not** increase
- [ ] Pathologies 1, 4 and 10 resolved
- [ ] Every timeout visible in the exception list

---

## Phase 4 — fuzzy matching and global assignment · **PASS** (2026-08-26)

**Gate evidence.** `231 passed, 1 skipped`. Before/after on one dataset SHA (`d93197db`):
auto-match **66.3% → 66.3%**, false matches **0.00% → 0.00%**, `UNCLASSIFIED` **4 → 0**,
pathology-7 refusal **0/8 → 8/8** declared with all four candidate pairings as evidence.

**Layer 3 bought exception-queue precision, not coverage, at zero cost.** 46 ledger rows reach
it and only 4 have any candidate; the other 42 have their counterpart already named in an
exception because their payments sit in batches Layer 2 could not resolve. Attributing an order
to an unreconciled payment does not reconcile it, so those are blocked upstream by design.

- [x] Candidate generation: exact amount equality, exact currency, causal ordering, date window
- [x] Cost matrix solved with `scipy.optimize.linear_sum_assignment` — never greedy (D-0002)
- [x] Ambiguity rule at the **pre-registered** margin of zero (D-0023), tested by **necessity**:
      forbid an assigned pair, re-solve, and if the optimum is unchanged nothing determined it.
      Catches global degeneracy that a per-row tie check would match and get wrong twice
- [x] `verifier.verify_pairing` enforces D-0024's contract; a mismatched amount is rejected
      however good its cost
- [x] **`UNCLASSIFIED` == 0**, three phases ahead of its Phase 5 target

**Pre-registered before any Layer 3 run, so neither can be fitted to dev:**

- **Ambiguity margin = 0 (exact ties only)** — D-0023. Changing it later requires publishing
  *both* settings' full metrics blocks, not just the kept one.
- **Verifier contract for Layer 3 = exact amount equality, zero tolerance** — D-0024. Cost
  decides which candidate is *proposed*; arithmetic decides whether it is *accepted*.
  `FINCTL_AMOUNT_TOLERANCE_PAISE` stays 0, so Layer 3 cannot produce a group whose money does
  not balance — only one whose money balances and whose counterparty is wrong. That residual
  attribution risk is what the before/after false-match rate measures.

**Two defects found before starting, both blocking a meaningful Phase 4 gate:**

- [ ] **Pathology 7 does not present the choice it claims to test.** The twins
      (`ml_000181`–`184` on dev) have **zero** same-amount gateway payments, so they are
      *unmatched*, not *ambiguous* — the honest exception today would be `MISSING_GATEWAY_ROW`.
      The gate "pathology 7 produces an `AMBIGUOUS` exception" cannot be met by a correct engine
      against this data. The generator must give the twins gateway counterparts so a genuine
      2x2 identical block exists.
- [ ] **M5 batch members are mislabelled pathology 7.** Phase 1's `MECHANISM_PATHOLOGY` mapped
      `multiple_subsets_explain_delta` to 7 on the grounds of "same principle" (`SPEC.md` §4.1
      says exactly that). But SPEC §5 pathology 7 is the record-level no-distinguishing-key
      case, so `P7 46/46` is dominated by 14 perfectly matchable M5 batch rows and reports on
      the wrong population. M5 batches should carry pathology 1; ambiguity is a *mechanism*
      property, not a pathology.

**Also needs moving:** Layer 2 currently emits the `TIMING_OUTSIDE_WINDOW` sweep for unclaimed
pool rows. That is a **terminal** classification and must run after every layer has had a
chance, or Layer 2 will claim rows Layer 3 needed as candidates.

- [ ] Candidate generation with amount tolerance and date windows
- [ ] Cost matrix solved with `scipy.optimize.linear_sum_assignment` — **not greedy** (D-0002)
- [ ] The ambiguity rule: best and second-best within the margin ⇒ refuse, emit `AMBIGUOUS`

**Gate:**
- [ ] Pathology 7 produces an `AMBIGUOUS` exception, **not a match**
- [ ] False-match rate reported **before and after** this phase
- [ ] **`UNCLASSIFIED` ≤ 9 records** — Layer 3 absorbs pathology 7's 4 into `AMBIGUOUS`

---

## Phase 5 — LLM adjudication behind the verifier · **PASS** (2026-08-26)

**Gate evidence.** `254 passed, 1 skipped`. Auto-match **66.3% → 76.2%**, false matches
**0.00% → 0.00%**. Parse calls **3 → 1** with 2 regexes promoted and nothing replayed;
explanation calls steady at 5. `1.43` calls per 100 records cold, `0.00` on replay.
`DEMO_MODE=1 make eval` completes from fixtures, and a cache miss raises rather than reaching
the network.

**Scope was fixed at three items and held there.** Five capabilities that looked tempting are
logged in `docs/OPEN_QUESTIONS.md` under "Out of scope" rather than built — including
LLM disambiguation of `AMBIGUOUS` refusals, which would actively make the system worse.

**The honesty caveat that matters:** no call has ever reached the API. No credential exists in
this environment, so every LLM figure came from `OfflineProposer`, fixtures are tagged
`offline_stub`, and the metrics block says so on the same line as the numbers.

Layer 4 sees only what survived Layers 1–3 — target **under 5%** of the batch.

- [ ] Parse unstructured bank narration: regex first, LLM only on formats regex missed
- [ ] When the LLM parses a new format it **emits a regex**, that regex is validated against
      the example and cached in `core/rules_cache.py`. The LLM writes rules; the rules do the
      work
- [ ] Classify why a record cannot match, into the closed enum
- [ ] Draft the human-readable explanation and suggested resolution per exception
- [ ] `core/verifier.py` — the **only** module permitted to approve a match
- [ ] Pydantic structured output, hard per-run call budget that fails loudly, bounded
      retries, per-call cost accounting, every response cached to `fixtures/llm/` by prompt
      hash. All source text treated as untrusted
- [ ] No `temperature` parameter — it returns HTTP 400 on the default model (D-0004)
- [ ] **`UNCLASSIFIED` == 0.** Layer 4's second job *is* classification: dispute legs →
      `DISPUTE_UNRESOLVED`, later-cycle refunds → `TIMING_OUTSIDE_WINDOW`, orphan adjustments
      → `UNEXPLAINED_ADJ`. Any residue is itemised record by record, never left in the bucket
- [ ] **Split `MISSING_BANK_ROW` from `UNPARSEABLE_NARRATION`.** Layer 1 cannot distinguish
      "no credit exists" from "a credit exists but its reference is unparseable" — both look
      identical to an exact-match layer, and the Phase 2 block shows M3 reporting
      `MISSING_BANK_ROW 2` for credits that are sitting right there with unreadable
      narration. Reading narration is Layer 4's job, so the split belongs here

**Gate:**
- [ ] `DEMO_MODE=1 make eval` completes with the **network disabled**, fixtures only
- [ ] Ablation table (deterministic / +fuzzy / +LLM) generated, false-match rate on every arm
- [ ] LLM calls per run **falling** across runs as the rules cache fills

---

## Phase 6 — UI, report, deploy · TODO

- [ ] One page, no framework, no build step: run header, the five-bar **cascade**, metrics
      strip, expandable exception table
- [ ] Monospace tabular numerals; money right-aligned, two decimals, formatted from integer
      paise at the last moment
- [ ] Grayscale plus at most two accents; sentence case; labels name what the user sees
      ("Could not match", not `LAYER_4_REJECT`)
- [ ] Visible keyboard focus, `prefers-reduced-motion` respected, readable at phone width
- [ ] `make report` renders a **static** `docs/index.html` with data inlined — no fetch
- [ ] `Dockerfile` (non-root, `/healthz`), `docker-compose.yml`, `render.yaml`
- [ ] `DEMO_MODE=1` in the deployed environment
- [ ] **Run the holdout evaluation, once, and report whatever it says**

**Gate:**
- [ ] Deployed URL responds
- [ ] `make demo` works from a clean clone with **no API key**

---

## Phase 7 — submission materials · TODO

- [ ] `README.md`: problem, architecture, one-command run, results table, honest limitations,
      both URLs at the very top
- [ ] The "what broke" writeup, from `WHAT_BROKE.md` entries written as they happened
- [ ] Final metrics, pasted
- [ ] An explicit list of what is **not** solved
