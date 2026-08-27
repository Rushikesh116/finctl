# FinCtl

| | |
|---|---|
| **Live API + UI** | **<https://finctl.onrender.com>** — [`/healthz`](https://finctl.onrender.com/healthz) · [`/api/run`](https://finctl.onrender.com/api/run) · [`/api/exceptions`](https://finctl.onrender.com/api/exceptions) |
| **Static report** | **<https://rushikesh116.github.io/finctl/>** — whole run inlined, no server, no fetch |

Free tier, so the first request after an idle period cold-starts. The static report is the
zero-infrastructure fallback: if the service is asleep, the numbers are still readable.

**An AI finance controller for payment reconciliation.** Given a merchant's order ledger, a
payment gateway's records, and a bank statement, FinCtl determines which records match,
which do not, and why — then reports **measured** accuracy on a held-out dataset.

The static report has the whole run inlined — no server, no fetch, no build step — so the
numbers stay readable whether or not the live service is awake.

> **Status: Phase 6 of 7.** Measured on `dev_seed_11`: **76.2% auto-matched, 0.00% false
> matches**, 133 exceptions, 0 unclassified. The held-out dataset is evaluated **once**, and
> that result is reported in `docs/METRICS.md` — not here, and not before it is run. Every
> number in this repo is pasted from command output.

## Run it

```bash
make setup
make demo     # seed + run + eval + report, from clean, with no API key set
```

`make demo` is the one command a judge runs. It works on a clean clone with no API key set,
because Layer 4's LLM responses replay from committed fixtures keyed by prompt hash. Verified
by actually cloning into an empty directory with no `.env` present, not by testing in the
working tree.

## Deploy

The container is the unit of deployment, and `render.yaml` only tells Render how to run it —
so the same image goes to Railway, Fly, or anything else that speaks Docker without change.

```bash
docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t finctl .
docker run -p 8010:8000 finctl          # then GET localhost:8010/healthz
```

Render picks up `render.yaml` as a Blueprint: **New → Blueprint → select this repo → Apply.**
Nothing else to configure, and no secret to paste — `DEMO_MODE=1` is the deployed default and
`GEMINI_API_KEY` is deliberately left unset (see the comment in `render.yaml`).

Two things the deployment gets right that are easy to get wrong:

- **The port comes from the platform.** `PORT` is honoured by both the server and the Docker
  health check. Render assigns `10000`; a health check pinned to `8000` would mark a working
  container unhealthy.
- **The image reports its own provenance.** It carries no `.git`, and no platform passes Docker
  build args from a blueprint, so `/healthz` reads the commit from `RENDER_GIT_COMMIT` or
  `RAILWAY_GIT_COMMIT_SHA`. A deployed service that cannot say which code produced its numbers
  is not auditable.

`/healthz` returns the dataset SHA and git SHA it is serving, not just `ok`, so a healthy
container serving stale data is distinguishable from one that is not. `GET /api/run` for the
whole run; `GET /api/exceptions` for the queue. `?dataset=holdout_seed_97` returns **403** by
design — the holdout is evaluated once and reported, and an endpoint that re-ran it on request
would turn it into a development set the first time anyone refreshed.

## The problem

Three sources describe the same money: the merchant's ledger, the gateway's records, and
the bank statement. Reconciling them is not a join, because **gateways settle in net
batches** — one bank credit is the arithmetic result of many payments netted against
refunds, fees, GST on those fees, chargebacks and adjustments. So reconciling a bank line
is **set reconstruction against a single scalar**, not row-to-row matching.

## Architecture

A four-layer cascade, each layer handing on only what it could not resolve:

| Layer | Job |
|---|---|
| 1 — exact | Match on bank reference, payment id, order id |
| 2 — netting | Check the settlement balance identity; if δ ≠ 0, **bounded** subset search for δ |
| 3 — fuzzy | Candidate generation, then globally optimal assignment. **Refuse** when best and second-best are within the margin |
| 4 — LLM | Adjudicate the residue (target < 5%), always behind an independent verifier |

Three things to know about it, all expanded in Phase 7:

- **The LLM proposes; a deterministic verifier disposes.** No LLM output reaches the matched
  ledger directly. A hallucinated match cannot enter the ledger, and prompt injection
  through untrusted bank narration cannot cause a false match — at worst it produces a
  proposal the verifier rejects.
- **The bounded search is a real stopping rule.** A node budget and a wall-clock timeout,
  both configurable, that dump to a typed exception on overflow rather than running forever.
- **Refusal is a feature.** Two customers, same amount, same day, no distinguishing key
  gets flagged `AMBIGUOUS` with an explanation. A system that confidently matches an
  ambiguous pair has great coverage and terrible precision.

## Results

TBD — Phase 6. The full metrics block, including the **false-match rate** and the
deterministic / +fuzzy / +LLM ablation table, will be pasted here verbatim from
`make eval`. Whatever the holdout prints is what ships, even if it is worse than dev.

## Limitations

TBD — Phase 7, and it will be specific. The honest ones already known:

- The settlement netting identity is FinCtl's construct; no gateway publishes a closed-form
  formula. Reported accuracy measures the matcher against *our model of the domain*
  (`docs/OPEN_QUESTIONS.md` Q-005).
- Pathologies 10, 11 and 12 rest on undocumented mechanics (Q-006, Q-007, Q-010).
- **`transfer` rows — Route split-payment legs — are out of scope.** They are a documented
  quarter of the recon `type` enum, but no fetched document defines their settlement
  semantics, so generating them would mean measuring accuracy against an invented shape
  (Q-008, D-0012).
- No live gateway call is made in any phase, in either direction.

## Repository map

See `CLAUDE.md` for the invariants, the pinned stack, every `make` target, and the
anti-hallucination protocol. `docs/SPEC.md` is the domain contract. `docs/DECISIONS.md`
records why each non-obvious choice was made. `docs/WHAT_BROKE.md` is the failure log.
