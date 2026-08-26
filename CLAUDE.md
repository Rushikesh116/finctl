# FinCtl

**An AI finance controller for payment reconciliation: given a merchant's order ledger, a
payment gateway's records, and a bank statement, determine which records match, which do
not, and why — then report measured accuracy on a held-out dataset.**

---

## Session protocol — do this first, every session

At the start of every session, before touching code, read in this order:

1. **`CLAUDE.md`** (this file) — the rules
2. **`docs/PROGRESS.md`** — which phase we are in and what the next action is
3. **`docs/OPEN_QUESTIONS.md`** — what is unverified or blocked

Then read the local skill that covers what you are about to change:

- `.claude/skills/razorpay-domain/SKILL.md` — before naming any gateway field
- `.claude/skills/money-invariants/SKILL.md` — before any arithmetic on an amount
- `.claude/skills/eval-protocol/SKILL.md` — before running the harness or writing a number

These files exist so a fresh context never re-derives a fact that was already verified,
and never invents one that was not.

**Current phase: Phase 4 complete. 66.3% auto-match, 0.00% false matches, UNCLASSIFIED at
zero. Next is Phase 5 — LLM adjudication behind the verifier.** `docs/PROGRESS.md` is authoritative; if it disagrees with this line, it
wins.

---

## The six invariants

Non-negotiable. Violating any of them silently invalidates the whole submission.
Re-read them at the start of every session.

1. **Money is integer paise. Never a float. Never a Decimal-to-float round trip.** All
   parsing, arithmetic, storage, and comparison happen in `int` paise. Formatting to
   rupees happens only in the presentation layer. Add a test that fails if `float`
   appears in any signature in `core/money.py`.

2. **The matcher must never read ground truth.** `core/` may not import anything from
   `data/generator.py` or any module exposing labels. Enforce this with an automated test
   that parses imports across `core/` and fails on violation. Ground-truth leakage is the
   single most likely way to accidentally produce fake results.

3. **The LLM proposes; a deterministic verifier disposes.** No LLM output is ever written
   to the matched ledger directly. The LLM returns a structured proposal (candidate match,
   evidence record IDs, confidence, reasoning). `core/verifier.py` re-checks the arithmetic
   independently and either approves or converts it into an exception. Two things follow,
   and both belong in the README: a hallucinated match cannot enter the ledger, and prompt
   injection through untrusted bank narration text cannot cause a false match — at worst it
   produces a proposal the verifier rejects.

4. **Runs are deterministic and replayable.** Same seed plus same input produces a
   byte-identical audit log. LLM responses are cached to fixtures on disk, keyed by a hash
   of the prompt, so replaying a run requires no network and no API key.

5. **No number appears in any document unless a command produced it.** See the
   anti-hallucination protocol below.

6. **Test mode only.** If any live gateway call is ever added, it must be test-mode
   credentials read from the environment. Never commit a key. Never write to a live system.

**Enforcement in place from Phase 0:**

| Invariant | Enforced by | Status |
|---|---|---|
| 1 | `tests/test_invariants.py::test_no_float_in_money_signatures` + `::test_money_module_never_calls_float` | **active** — `core/money.py` landed in Phase 1 |
| 2 | `tests/test_invariants.py::test_core_never_imports_ground_truth` | active |
| 6 | `.githooks/pre-commit` → `scripts/check_secrets.py`, installed by `make setup` | active |

---

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Creates `.venv` on `python3.13`, installs the pinned deps from `requirements.txt`, and points `core.hooksPath` at `.githooks` so the secret scan runs on every commit |
| `make seed` | Generates both datasets — `dev_seed_11` and `holdout_seed_97` — into `data/generated/`, deterministically from their seeds |
| `make run` | Reconciles the dev dataset and writes the match ledger plus the hash-chained audit log to SQLite |
| `make eval` | The harness on the **dev** dataset plus the ablation table. Prints the metrics block that gets pasted into `docs/METRICS.md` |
| `make eval-holdout` | Phase 6 **only**, once: also evaluates the holdout. Deliberately not part of `make eval` — iterating against a holdout converts it into a training set |
| `make report` | Renders the static run report to `docs/index.html`, data inlined — no server, no fetch, no build step |
| `make serve` | Runs the FastAPI app on `:8000`, serving both the JSON API and the UI from one process |
| `make test` | Runs pytest |
| `make demo` | `seed` + `run` + `eval` + `report` in one command, from clean, **with no API key set**. This is what a judge runs |
| `make clean` | Removes generated datasets, run databases, and caches |

Override defaults with `make eval DEV_DATASET=... HOLDOUT_DATASET=...` or
`make setup BOOTSTRAP_PYTHON=python3.12`.

---

## Pinned stack

Do not add a dependency beyond this list without asking first.

| Purpose | Choice | Installed version | Why this one |
|---|---|---|---|
| Language | Python 3.13 | 3.13.14 | Brief says 3.11+; 3.11 is not installed on this machine, and matching the local interpreter to the container avoids a dev/prod skew (D-0001) |
| Records | `dataclasses` + `pydantic` v2 | 2.13.4 | Typed, reviewable, no dataframe magic hiding bugs |
| CSV / JSON ingest | stdlib `csv`, `json` | — | Deliberately no pandas: 5k records does not need it, and a reviewer can read plain loops |
| Assignment problem | `numpy` + `scipy.optimize.linear_sum_assignment` | 2.5.2 / 1.18.1 | Globally optimal matching, not greedy |
| Storage | stdlib `sqlite3`, plain SQL | — | Zero infra, file is committable, a judge can inspect it |
| API | `fastapi` + `uvicorn` | 0.141.1 / 0.52.4 | |
| LLM | `anthropic` SDK | 1.0.0 | |
| Tests | `pytest` | 9.1.1 | |
| Frontend | Vanilla HTML + CSS + JS, **no build step** | — | One container, no npm, no bundler, trivial deploy |

Versions were read from `pip show` after installing into `.venv`, never from memory.
`requirements.txt` holds the direct pins; `requirements.lock.txt` holds the full
transitive closure.

**Default model: `claude-opus-5`.** `temperature` is *not* available on it — see D-0004.

---

## Anti-hallucination protocol

1. **Never invent an external API's field names, endpoints, or semantics.** Fetch the
   documentation. If you cannot fetch it, write the assumption in the code as
   `# UNVERIFIED: <what and why>`, add it to `docs/OPEN_QUESTIONS.md`, and tell me. Do not
   quietly guess a plausible field name.
2. **Never write a number in any document that a command did not produce.** Every metric
   is pasted from stdout, with the command and git SHA above it. If you want to describe a
   result you have not run, write `TBD`.
3. **Never claim a test passes without running it.** Show the command and its output.
4. **Never guess a library's API.** Check the installed version and read the actual signature.
5. **Write the decision before the code.** Any non-obvious choice gets a `DECISIONS.md`
   entry first.
6. **If a phase gate cannot be met, say so and stop.** Do not lower the criterion, do not
   fake the result, do not proceed. Write it in `OPEN_QUESTIONS.md`.
7. **Treat a suspiciously good result as a bug.** If auto-match exceeds 99% or false
   matches hit exactly zero, assume ground-truth leakage or a tolerance set too wide, and
   investigate before celebrating.

---

## Stop and ask before

- Adding any dependency not in the pinned stack table above.
- Changing `docs/SPEC.md` after the Phase 1 freeze.
- Making any network call to a payment gateway, test mode or otherwise.
- Touching `holdout_seed_97` for anything other than the single final evaluation.
- Restructuring the layer cascade or the verifier boundary.

---

## Architecture in one screen

Three sources describe the same money: the **merchant ledger** (orders sold, refunds
issued), the **payment gateway** (payments captured, refunds, fees, GST on fees,
chargebacks, adjustments, settlements), and the **bank statement** (credits and debits
that actually hit the account).

This is not a simple join, because **gateways settle in net batches.** One bank credit is
the arithmetic result of potentially hundreds of payments netted against refunds, fees,
GST, chargebacks and adjustments. Reconciling a bank line is **set reconstruction against
a single scalar**, not row-to-row matching. That distinction is the core of the project.

The cascade, each layer handing on only what it could not resolve:

| Layer | Module | Job |
|---|---|---|
| 1 | `core/identity.py` | Exact match on bank reference, payment id, order id |
| 2 | `core/settlement.py` | Check the balance identity; if δ ≠ 0, **bounded** subset search for δ. Iterative deepening by subset size; minimal explanation wins; ties are refusals (D-0021) |
| 3 | `core/assignment.py` | Candidate generation, then `linear_sum_assignment` for a globally optimal one-to-one assignment. Refuse to match when best and second-best fall within the margin |
| 4 | `core/adjudicate.py` | LLM on the residue (target < 5%): parse narration, classify, explain — always behind `core/verifier.py` |

`audit/ledger.py` records every decision — layer, inputs, output, confidence, timestamp,
and model version plus token cost when an LLM was involved — in a hash-chained
append-only log.

**The bounded search in Layer 2 is the stopping rule.** A node budget and a wall-clock
timeout, both configurable, that dump to a typed exception on overflow rather than running
forever. Say so explicitly in the README: the track asks for stopping rules and most
submissions will not have a real one.

Two deliberate choices to defend in the README:

- **Global assignment, not greedy.** Greedy matching starves correct pairings and inflates
  false matches (D-0002 records this).
- **Refusal is a feature.** A system that confidently matches an ambiguous pair has great
  coverage and terrible precision. Pathology 7 — two customers, same amount, same day, no
  distinguishing key — must produce an `AMBIGUOUS` exception with an explanation, not a
  coin flip.

---

## Where things live

```
core/       the matcher. May not import data/ or eval/ (invariant 2)
data/       generator + scenario config + ground-truth labels
audit/      hash-chained decision log
eval/       harness, metrics, ablation
api/        FastAPI app: JSON API + static UI from one process
web/        vanilla frontend assets, no build step
fixtures/   llm/ = recorded LLM responses by prompt hash; demo/ = DEMO_MODE artefacts
docs/       SPEC, DECISIONS, PROGRESS, OPEN_QUESTIONS, METRICS, WHAT_BROKE, index.html
scripts/    report renderer, secret scanner
```

Dependency arrow points one way: `data/` and `eval/` import record schemas **from**
`core/`. `core/` imports neither. That is what makes invariant 2 mechanically checkable.

---

## Keep the failure log honest

`docs/WHAT_BROKE.md` is append-only, written **as it happens**, not reconstructed at the
end: symptom, diagnosis, fix, and the metric before and after. The submission form asks
"what broke, and how you got out," and the organisers read that answer first. Capture the
numbers on both sides of a fix while you still remember them.
