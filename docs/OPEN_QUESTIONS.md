# Open questions

Everything unverified or blocked, with what would resolve it. Read this at the start of
every session. An item here is never a reason to guess: it is either resolved, or the
assumption is written into the code as `# UNVERIFIED: <what and why>` and mirrored here.

**Nothing is blocking Phase 1.** Q-001, Q-008 and Q-013 were resolved at the Phase 0 review
on 2026-08-26; Q-002 was resolved far enough to freeze `SPEC.md` §4. The remaining items are
domain facts no fetched document answers — each is carried as a stated assumption, not a
guess.

---

## Q-001 — `data/scenarios.yaml` needs a YAML parser, which is not in the pinned stack

**Status: RESOLVED 2026-08-26 — `data/scenarios.toml` via stdlib `tomllib` (D-0010).**

The brief asks for `data/scenarios.yaml` (Phase 1) holding the twelve pathologies and their
mix weights. Nothing in the §3.2 stack table can parse YAML — there is no `pyyaml`, and
Python has no YAML in the standard library. Adding a dependency requires approval
(`CLAUDE.md` → "Stop and ask before"), so this has not been done.

**Options.**

1. **`data/scenarios.json`, no new dependency.** stdlib `json`, consistent with the brief's
   "deliberately no pandas" instinct of keeping the reviewable surface small. Costs
   comments — JSON has none — which matters for a config a judge might read.
2. **Add `pyyaml`.** One well-known dependency; keeps the filename and the comments the
   brief implies.
3. **`data/scenarios.toml`, no new dependency.** Python 3.11+ ships `tomllib` in the
   stdlib. Supports comments, and reads well for a flat weights table. Filename differs
   from the brief.

**Chosen: option 3.** Comments, zero new dependencies, stdlib parser. The filename deviates
from the brief's `.yaml`; the pathology mix keeps its inline commentary, which matters for a
config a judge may read. No dependency was added to `requirements.txt`.

---

## Q-002 — Does the settlement netting subtract `fee` and `tax` separately, or `fee` alone?

**Status: RESOLVED for the SPEC freeze 2026-08-26 (D-0011). Still open for the real-API
ingestion adapter, which needs a real settlement statement.**

`SPEC.md` §4 freezes on the brief's reading: `fee_base` and `gst` are subtracted
**separately**, which also matches the dashboard break-up. The generator satisfies that
identity by construction, and the README states it as an assumption rather than a
documented contract. Only the adapter that ingests a real Payment entity still depends on
the answer below.

The gateway's own documentation reads two ways:

- Payment entity `fee`: *"Fee (including GST) charged by Razorpay."* `tax`: *"GST charged
  for the payment."* → `fee` is GST-**inclusive**, so net = `amount − fee`.
- Dashboard settlement break-up: *"Payment - Adjustment - Tax - Fee"* → Tax and Fee are
  **separate** deductions, so net = `amount − fee − tax`.

The two differ by exactly the GST. Choosing wrong shifts every settlement δ by 18/118 of
the fee: small enough to look like a rounding bug, large enough to fail every balance check.

**Mitigated by design.** D-0003 makes FinCtl's canonical schema
(`fee_base_paise` + `gst_paise`) correct under **either** reading, so nothing downstream of
the generator is at risk from the ambiguity.

**Resolves when:** a real settlement statement or recon report is inspected, or Razorpay
support confirms. Until then the README says so plainly. Related: `razorpay-domain` → U-2.

---

## Q-003 — `temperature=0` is not available on the default model

**Status: decided as D-0004; open only if the user wants literal compliance.**

The brief specifies temperature 0 for Layer 4. Verified 2026-08-26: `temperature` has been
**removed on Opus 5, Sonnet 5, Fable 5, and Opus 4.7/4.8** and returns HTTP 400. Determinism
therefore comes from the fixture cache (invariant 4), which was always the real mechanism.

**If strict compliance matters**, `claude-haiku-4-5` still accepts `temperature`, at the cost
of model capability on the adjudication task.

**Resolves when:** the user confirms the fixture-cache approach, or asks for the model swap.

---

## Q-004 — No verified USD→INR rate for `Cost / 1000  Rs …`

The metrics block reports cost per 1000 records **in rupees**. Token pricing is verified
($5/$25 per MTok for `claude-opus-5`, from the models overview page, 2026-08-26), but an FX
rate is not, and it moves daily.

**Handled, not guessed.** `FINCTL_USD_INR` is deliberately **empty** in `.env.example`. With
no rate set the harness prints the USD figure and `Rs TBD`. It must never print a
plausible-looking INR number from a guessed rate.

**Resolves when:** the user pins a rate (and we record the date and source alongside it), or
we agree the block reports USD.

---

## Q-005 — There is no published settlement netting formula

The netting identity in `SPEC.md` §4 is a **FinCtl construct**, not a documented gateway
contract. Razorpay's docs state settlement happens "after deducting fees" and the dashboard
shows an illustrative break-up, but no closed-form identity exists in any fetched page.

**Consequence to state plainly in the README:** the generator satisfies this identity by
construction, so the reported accuracy measures the matcher against *our model of the
domain*, not against production ground truth. That is an honest limitation, and naming it
is worth more than hiding it.

**Resolves when:** a real recon report is reconciled by hand against a real bank credit.

---

## Q-006 — On-hold release mechanics are undocumented (pathology 10)

The docs say a settlement can be held "if we detect some risk" and that you "contact the
support team" to release it. They do not say what happens to the held balance, when it
releases, or whether the released amount appears in a later settlement.

FinCtl assumes: held balance is released into a later batch, visible as a row whose
`on_hold` is false and whose `settled_at_utc` falls in a later batch, and the identity gets
a `+ Σ on_hold_released` term. **`UNVERIFIED`.**

**Resolves when:** a real held-then-released settlement is observed. Related: U-4.

---

## Q-007 — Can an `adjustment` exist with no order or payment reference? (pathology 11)

The docs define adjustments only as "Adjustments to transactions, if any" and are silent on
whether a reference is required. Pathology 11 — "genuine unresolvables" — depends on the
answer: if every adjustment carries a reference, the pathology is fiction and the
`UNEXPLAINED_ADJ` exception type is measuring nothing.

**Resolves when:** a real recon report containing an adjustment row is inspected. Related: U-5.

---

## Q-008 — Two `type` values in the recon enum have no pathology coverage

The documented enum is `payment`, `refund`, `transfer`, `adjustment`. The twelve pathologies
exercise payments, refunds and adjustments, but **never `transfer`** (Razorpay Route
split-payment legs). Either the datasets under-represent a real quarter of the enum, or
transfers are out of scope and the README should say so.

**Status: RESOLVED 2026-08-26 — transfers are out of scope (D-0012).** Stated explicitly in
the README's limitations rather than papered over with a 13th pathology whose semantics no
fetched document defines. Cheap to state, expensive to fake.

---

## Q-009 — Marketplace plugin installs need a human

The brief suggests `/plugin install security-guidance@claude-code-plugins` and
`commit-commands@claude-code-plugins`. Verified in this environment: the CLI does expose
`claude plugin install [--scope project|user|local]`, and one marketplace is configured —
but it is named **`claude-plugins-official`** (source `anthropics/claude-plugins-official`),
not `claude-code-plugins`, so the names in the brief will not resolve as written. No plugins
are currently installed.

Installing into the user's config is their call, not mine. **Suggested:**
`claude plugin install security-guidance@claude-plugins-official --scope project` — and if
that name does not resolve, browse with `/plugin` and pick the nearest equivalent rather
than inventing a name.

**Resolves when:** the user runs it, or says skip.

---

## Q-010 — How is an FX payment's INR credit represented? (pathology 12)

Confirmed from docs: international cycles differ from the T+2 domestic default, and
non-INR currencies have different minor-unit scales (JPY passes `295` for ¥295). **Not**
documented: the conversion representation — where the rate lives, whether a spread is
disclosed separately, and which side rounds.

FinCtl stores an integer `fx_rate_micros` and the settled INR amount, converting once and
storing the result. **`UNVERIFIED`** — the README must say pathology 12 is generated under a
stated assumption. Related: U-7.

---

## Q-011 — How does a representment reversal appear? (pathology 5)

Only `amount_deducted` on a `lost` dispute is documented — *"deducted from your Razorpay
current balance when the dispute is `lost`"*. Whether a `won` representment returns the money
as a credit row, an adjustment, or a silent status change is not stated. There is no
`chargeback` value in the recon `type` enum, so a dispute surfaces via `dispute_id` on a row.

FinCtl generates the reversal as a credit leg referencing the same `dispute_id`.
**`UNVERIFIED`.** Related: U-8.

---

## Q-012 — Bank narration formats are unspecifiable

`NEFT-RAZORPAYSOFTWARE-UTR8837261-XYZ` is a **bank** artefact, not a gateway one. No gateway
doc specifies it and it varies per bank, per channel, and over time.

This is not really a blocker — it is the *justification* for Layer 4's design. Regex first;
the LLM only on formats regex missed; when the LLM parses a new format it emits a regex,
that regex is validated against the example and cached, and it runs deterministically from
then on. The LLM writes rules; the rules do the work. Related: U-6.

---

## Q-014 — Does the gateway's row-level writeback actually lag the money movement?

`SPEC.md` §4.1 mechanism **M1 (export cutoff skew)** is the primary reason δ ≠ 0 in the
generated data, so it carries a lot of weight: rows settled near the export cutoff appear
with `settled=false` and null settlement fields even though the bank credit for their batch
has already posted.

**What is grounded.** The documented settlement `status` enum is `created | processed |
failed`, and `utr` is described as coming from the bank ("available across banks") — so a
settlement genuinely can exist before its UTR does. And source systems with different
write-back latencies producing an internally inconsistent period-boundary export is a
universal finance-ops problem, not a novel invention.

**What is not.** Whether *this* gateway specifically leaves rows in that state, and for how
long. No fetched page describes export cutoff behaviour.

**Why it is acceptable to build on.** M1 is a statement about the *input data FinCtl is
handed*, not about gateway internals. Any merchant pulling a recon report at a period
boundary faces some version of it; FinCtl's claim is that it handles rows whose settlement
assignment is pending, which is true regardless of the precise upstream cause. The README
names it as a stated assumption.

**Resolves when:** a real recon report is pulled across a settlement boundary and the row
states inspected.

---

## Q-013 — Dockerfile base image differs from the brief

**Status: RESOLVED 2026-08-26 — confirmed by the user. Keep 3.13 everywhere.**

Per D-0001: `python:3.13-slim` rather than `python:3.11-slim`, matching the local
interpreter so there is no dev/prod skew. This is a knowing deviation from the brief's
literal text, and the Dockerfile in Phase 6 will carry a comment saying so.
