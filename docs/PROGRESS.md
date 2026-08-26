# Progress

Phase checklist with acceptance criteria and pass/fail state. Updated at the end of every
phase. This file is authoritative on "which phase are we in" — if `CLAUDE.md` disagrees,
this file wins.

> ## Next action
>
> **Phase 2 — normalisation, Layer 1, the audit ledger, and harness v1.**
>
> Build order: `core/normalize.py` → `core/identity.py` → `audit/ledger.py` →
> `eval/harness.py`. Then paste the baseline metrics block into `docs/METRICS.md` with its
> command and git SHA. Expect roughly 85% on exact matching alone; **whatever it prints is
> the number every later phase is measured against**, so it gets recorded before anything is
> tuned.
>
> `docs/SPEC.md` is **frozen** as of the Phase 1 gate. Changing it needs a `DECISIONS.md`
> entry and approval.
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

## Phase 2 — normalisation, Layer 1, audit ledger, harness v1 · TODO

- [ ] `core/normalize.py` — schema mapping, the UTC/IST asymmetry (`SPEC.md` §3.4), currency
      normalisation
- [ ] `core/identity.py` — Layer 1 exact matching on bank reference, payment id, order id
- [ ] `audit/ledger.py` — append-only, **hash-chained**; every decision records layer,
      inputs, output, confidence, timestamp, and model version + token cost when an LLM was
      involved
- [ ] `eval/harness.py` — prints the full metrics block, even though most layers do not
      exist yet

**Gate:**
- [ ] `make eval` prints real numbers with only exact matching enabled (expect roughly 85%)
- [ ] `auto_matched + exception_records == N` asserted
- [ ] Baseline pasted into `docs/METRICS.md` with command + SHA. **This is what every later
      improvement is measured against**

---

## Phase 3 — settlement decomposition · TODO

The hardest algorithmic piece.

- [ ] Balance-identity check first; `δ == 0` reconciles the whole batch at once
- [ ] Bounded subset search for δ: node budget **and** wall-clock timeout, both configurable
- [ ] Overflow becomes a typed exception (`SUBSET_SEARCH_EXHAUSTED`), never a silent drop
- [ ] **≥2 distinct closing subsets ⇒ refuse with `AMBIGUOUS`**, carrying the §4.2 evidence
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

## Phase 4 — fuzzy matching and global assignment · TODO

- [ ] Candidate generation with amount tolerance and date windows
- [ ] Cost matrix solved with `scipy.optimize.linear_sum_assignment` — **not greedy** (D-0002)
- [ ] The ambiguity rule: best and second-best within the margin ⇒ refuse, emit `AMBIGUOUS`

**Gate:**
- [ ] Pathology 7 produces an `AMBIGUOUS` exception, **not a match**
- [ ] False-match rate reported **before and after** this phase

---

## Phase 5 — LLM adjudication behind the verifier · TODO

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
