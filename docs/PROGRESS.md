# Progress

Phase checklist with acceptance criteria and pass/fail state. Updated at the end of every
phase. This file is authoritative on "which phase are we in" — if `CLAUDE.md` disagrees,
this file wins.

> ## Next action
>
> **Phase 1 is unblocked. Start at `core/money.py`.**
>
> Build order within Phase 1, each step testable before the next: `core/money.py` →
> `core/records.py` → `data/scenarios.toml` → `data/generator.py` → both datasets →
> `data/DATASET_HASHES.txt` → freeze `SPEC.md`.
>
> Write `core/money.py` first and get the two skipped invariant tests to run. Everything
> downstream inherits its correctness, and a generator built on wrong money arithmetic makes
> every later metric a lie.
>
> Settled at the Phase 0 review (2026-08-26): **Q-001** → `scenarios.toml` via stdlib
> `tomllib` (D-0010). **Q-002** → `SPEC.md` §4 freezes on separate `fee_base` + `gst`
> subtraction, flagged as an assumption (D-0011). **Q-008** → `transfer` rows out of scope
> (D-0012). **Q-013** → Python 3.13 everywhere, confirmed (D-0001).
>
> Still outstanding, none blocking: **Q-004** (USD→INR rate; harness prints `Rs TBD` until
> set), **Q-005/Q-006/Q-007/Q-010/Q-011** (domain facts no fetched doc answers — carried as
> stated assumptions), **Q-009** (plugin installs need a human; the marketplace here is
> `claude-plugins-official`, so the brief's names will not resolve as written).

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

## Phase 1 — money and the data generator · **READY** (unblocked 2026-08-26)

The most important phase: if the generator is wrong, every metric downstream is a lie.

- [ ] `core/money.py` — integer paise type, parsing, formatting, `split_with_remainder`
- [ ] `core/records.py` — canonical schemas per `SPEC.md` §3 (`core` owns them; `data` and
      `eval` import *from* `core`)
- [ ] `data/scenarios.toml` — twelve pathologies with mix weights, stdlib `tomllib` (D-0010)
- [ ] `data/generator.py` — three sources **plus** a separate ground-truth labels file
- [ ] `dev_seed_11` (~500 records) and `holdout_seed_97` (~500, different seed)
- [ ] `data/DATASET_HASHES.txt` — committed SHA-256 manifest (D-0007)

**Gate:**
- [ ] `make seed` twice yields identical file hashes
- [ ] A test asserts every pathology appears **at least twice** in **each** dataset
- [ ] Both forms of the netting identity (`SPEC.md` §4) agree on every generated batch
- [ ] `split_with_remainder` has a **property test**: `sum(parts) == total` over random inputs
- [ ] `test_no_float_in_money_signatures` and `test_money_module_never_calls_float` **no
      longer skip**
- [ ] `docs/SPEC.md` frozen

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
