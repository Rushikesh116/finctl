---
name: razorpay-domain
description: Verified Razorpay entity schemas — exact field names, ID prefixes, units and timestamp semantics for orders, payments, refunds, settlements, disputes and the settlement recon report. Read this before writing or changing any code that names a gateway field, and before adding a field to the generator or the normaliser. Every fact is either quoted from fetched documentation with its URL, or explicitly marked UNVERIFIED.
---

# Razorpay domain schemas

**Provenance rule for this file:** every VERIFIED row below was fetched from
`razorpay.com/docs` on **2026-08-26** and is quoted, not paraphrased from memory.
Anything not in the fetched pages is marked `UNVERIFIED` and mirrored into
`docs/OPEN_QUESTIONS.md`. Do not add a row here from recollection. If you need a field
that is not listed, fetch the page, add the quote and the URL, or add an
`UNVERIFIED` row — never a plausible-looking guess.

## Universal conventions (VERIFIED)

| Convention | Documented wording | Applies to |
|---|---|---|
| Money units | "Payment amount in the smallest currency sub-unit. For example, if the amount to be charged is ₹299, then pass `29900`" | every `amount`, `fee`, `tax`, `debit`, `credit` field |
| Timestamps | "Unix timestamp" — an `integer`, epoch seconds, UTC | every `created_at`, `settled_at`, `respond_by` |
| Currency | 3-letter ISO code, `string` | every `currency` field |
| Notes | "A maximum of 15 key-value pairs", 256 chars each | `notes` on orders, payments, refunds |

Zero-decimal and three-decimal currencies exist and do **not** use paise: the Payments
entity page documents JPY passing `295` for ¥295, and three-decimal currencies passing
`295990`. Relevant to pathology 12 (FX). See `money-invariants` for how FinCtl stores
non-INR amounts.

## Order (VERIFIED — `/docs/api/orders/entity`, `/docs/api/orders/create`)

`id` prefix `order_`, example `order_RB58MiP5SPFYyM`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | unique identifier of the order |
| `amount` | integer | smallest currency sub-unit |
| `amount_paid` | integer | "The amount paid against the order." |
| `amount_due` | integer | "The amount pending against the order." |
| `currency` | string | ISO code, 3 characters |
| `receipt` | string | "max 40 characters and must be unique" |
| `status` | string | `created` → `attempted` → `paid` |
| `attempts` | integer | "successful and failed" attempts against this order |
| `notes` | json object | max 15 pairs |
| `created_at` | integer | Unix timestamp |

## Payment (VERIFIED — `/docs/api/payments/entity`)

`id` prefix `pay_`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | prefix `pay_` |
| `amount` | integer | smallest currency sub-unit |
| `currency` | string | supports international currencies |
| `status` | string | `created`, `authorized`, `captured`, `refunded`, `failed` |
| `order_id` | string | "Order id, if provided." — **may be absent** |
| `international` | boolean | international vs domestic card |
| `method` | string | `card`, `netbanking`, `wallet`, `emi`, `upi` |
| `amount_refunded` | integer | in currency subunits |
| `refund_status` | string | `null`, `partial`, `full` |
| `captured` | boolean | whether the payment is captured |
| `fee` | integer | **"Fee (including GST) charged by Razorpay."** |
| `tax` | integer | **"GST charged for the payment."** |
| `error_code` | string | e.g. `BAD_REQUEST_ERROR` |
| `error_source` | string | e.g. `customer` |
| `error_step` | string | e.g. `payment_authentication` |
| `error_reason` | string | e.g. `incorrect_otp` |
| `acquirer_data` | array | "a dynamic array consisting of unique reference numbers" |
| `created_at` | integer | Unix timestamp |

> **The single most dangerous field pair in this project.** At the Payment entity,
> `fee` is documented as **inclusive of** GST and `tax` is the GST component *inside*
> it. Subtracting both from a gross amount double-counts GST. See
> `money-invariants` → "The fee/GST trap" and `docs/OPEN_QUESTIONS.md` Q-002 before
> writing any netting arithmetic.

## Refund (VERIFIED — `/docs/api/refunds/entity`)

`id` prefix `rfnd_`, example `rfnd_FgRAHdNOM4ZVbO`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | prefix `rfnd_` |
| `amount` | integer | "The amount to be refunded (in the smallest unit of currency)" |
| `currency` | string | |
| `payment_id` | string | the payment being refunded, e.g. `pay_FgR9UMzgmKDJRi` |
| `notes` | json object | max 15 pairs |
| `receipt` | string | "A unique identifier provided by you for your internal reference." |
| `acquirer_data` | array | "Reference number (RRN, ARN, or UTR) from banking partner" |
| `created_at` | integer | Unix timestamp, e.g. `1600856650` |
| `batch_id` | string | "populated if the refund was created as part of a batch upload" |
| `status` | string | `pending`, `processed`, `failed` |
| `speed_processed` | string | `instant`, `normal` |
| `speed_requested` | string | `normal`, `optimum` |

A refund always carries `payment_id`, so refund→payment linkage is exact-matchable at
Layer 1. Whether the refund lands in the *same* settlement batch as its payment is a
separate question — that is pathology 4 (cross-batch clawback).

## Settlement (VERIFIED — `/docs/api/settlements/entity`)

`id` prefix `setl_`, example `setl_7IZKKI4Pnt2kEe`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | prefix `setl_` |
| `entity` | string | literal `settlement` |
| `amount` | integer | "The amount to be settled (in the smallest unit of currency)" |
| `status` | string | `created`, `processed`, `failed` |
| `fees` | integer | total fee for all payments in this settlement. **"In case of a normal settlement the fee charge will be `0`."** |
| `tax` | integer | "total tax … charged on the fees". **Also `0` for a normal settlement.** |
| `utr` | string | "The Unique Transaction Reference (UTR) number available across banks, which can be used to track a particular settlement in your bank account. For example, `1597813219e1pq6w`." |
| `created_at` | integer | Unix timestamp |

Two consequences worth holding on to:

1. **For a normal settlement, `fees` and `tax` on the Settlement entity are zero.** Per-
   transaction fees live on the payment rows, not the settlement header. So the netting
   arithmetic must be reconstructed from the recon report, not read off the settlement.
2. **`utr` is the join key to the bank statement.** It is the one field that appears on
   both sides of the gateway↔bank boundary, which makes it Layer 1's primary key —
   and makes pathology 2 (a reused bank reference) an attack on exactly that key.

## Settlement recon report (VERIFIED — `/docs/api/settlements/fetch-recon/`)

"A list of all transactions such as payments, refunds, transfers and adjustments
settled to your account on a particular day or month."

| Field | Type | Notes |
|---|---|---|
| `entity_id` | string | "The unique identifier of the transaction that has been settled." |
| `type` | string | **`payment`, `refund`, `transfer`, `adjustment` — these four only.** `transfer` is **out of scope** for FinCtl (D-0012) |
| `debit` | integer | "The amount, in currency subunits, that has been debited from your account." |
| `credit` | integer | "The amount, in currency subunits, that has been credited to your account." |
| `amount` | integer | "The total amount … debited or credited from your account." |
| `currency` | string | 3-letter ISO |
| `fee` | integer | "The fees … charged to process the transaction." |
| `tax` | integer | "The tax on the fee … charged to process the transaction." |
| `on_hold` | boolean | "Indicates whether the account settlement for transfer is on hold." |
| `settled` | boolean | "Indicates whether the transaction has been settled or not." |
| `created_at` | integer | Unix timestamp of the transaction |
| `settled_at` | integer | Unix timestamp when it was settled |
| `settlement_id` | string | the settlement it belongs to |
| `payment_id` | string | "the payment linked to `refund` or `transfer` that has been settled" |
| `settlement_utr` | string | "The unique reference number linked to the settlement." |
| `order_id` | string | order behind the settled payment |
| `order_receipt` | string | receipt entered at order creation |
| `method` | string | `card`, `netbanking`, `wallet`, `upi`, `emi` |
| `card_network` | string | `American Express`, `Diners Club`, `Maestro`, `MasterCard`, `RuPay`, `Visa`, `unknown` |
| `card_issuer` | string | "a 4-character code denoting the issuing bank" |
| `card_type` | string | `credit`, `debit` |
| `dispute_id` | string | "The unique identifier of any dispute, if any, for this transaction." |

Three structural facts that shape the generator and the matcher:

- **`debit` and `credit` are separate non-negative integers.** Direction is carried by
  which field is populated, not by a sign on `amount`. FinCtl mirrors this rather than
  inventing signed amounts — see `money-invariants` → sign conventions.
- **There is no `chargeback` value in the `type` enum.** A chargeback surfaces as a row
  with a populated `dispute_id`, not as its own type. Pathology 5 must be generated that
  way, or the dataset teaches the matcher a shape that does not exist in production.
- **There is no `on_hold_released` type either.** `on_hold` is a boolean on a row. The
  release of held balance into a later batch (pathology 10) is inferred from a row whose
  `on_hold` flips false and whose `settled_at` falls in a later batch — this inference is
  `UNVERIFIED`, see Q-006.

## Dispute / chargeback (VERIFIED — `/docs/api/disputes/entity`)

`id` prefix `disp_`, example `disp_AHfqOvkldwsbqt`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | prefix `disp_` |
| `entity` | string | literal `dispute` |
| `payment_id` | string | disputed payment |
| `amount` | integer | "Amount, in currency subunits, for which the dispute was created" |
| `currency` | string | |
| `amount_deducted` | integer | "The amount … deducted from your Razorpay current balance when the dispute is `lost`" |
| `reason_code` | string | |
| `respond_by` | integer | Unix timestamp deadline |
| `status` | string | `open`, `under_review`, `won`, `lost`, `closed` |
| `phase` | string | `fraud`, `retrieval`, `chargeback`, `pre_arbitration`, `arbitration` |
| `created_at` | integer | Unix timestamp |

`amount_deducted` is only non-zero when the dispute is `lost` — so a representment
reversal (pathology 5) is a status transition to `won` with the deduction returned. The
exact mechanics of the *return* leg are `UNVERIFIED` (Q-011).

## Settlement cycle (VERIFIED — `/docs/payments/settlements/`)

- Domestic default: "**T+2** working days (where **T** is the date of transaction capture)."
- Bank holidays: settlement processes "on the next working day after the bank holiday."
- International: cycle "varies by region", follows "applicable law(s)".

This is what makes pathology 9 (a T+5 settlement across a bank holiday) legitimate rather
than a bug: the date window in Layer 3 must be elastic, and elasticity must be justified
by the holiday calendar rather than widened until the numbers look good.

## UNVERIFIED — must not be presented as documented

Each of these is mirrored in `docs/OPEN_QUESTIONS.md` with what would resolve it.

| # | Assumption FinCtl makes | Why it is unverified | Q |
|---|---|---|---|
| U-1 | The settlement netting identity `credit = Σcaptured − Σrefunds − Σfees − ΣGST − Σchargebacks − Σadjustments + Σon_hold_released` | Razorpay publishes no closed-form netting formula. The dashboard page shows an illustrative break-up reading `Payment - Adjustment - Tax - Fee` and states settlement occurs "after deducting fees", but never a complete identity. FinCtl's identity is a **project construct**, not a documented contract. | Q-005 |
| U-2 | Whether the netting subtracts `fee` and `tax` **separately** or `fee` alone | Payment entity says `fee` is GST-inclusive; the dashboard break-up lists Tax and Fee as separate deductions. The two readings differ by the GST amount. **FinCtl freezes on separate subtraction (D-0011)**; only a real-Payment-entity ingestion adapter still depends on the answer. | Q-002 |
| U-3 | GST is 18% of the base fee, rounded half-up to the nearest paisa | The 18% rate is Indian statute, not a Razorpay API guarantee; the docs never state a rounding direction. | Q-002 |
| U-4 | Held balance is released into a later settlement batch, visible as `on_hold` flipping false | Docs say only that a hold happens "if we detect some risk" and that you "contact the support team" to release it. Release mechanics and batch visibility are undocumented. | Q-006 |
| U-5 | An `adjustment` row can exist with no `order_id` or `payment_id` | The docs define adjustments only as "Adjustments to transactions, if any" and are silent on whether a reference is required. Pathology 11 depends on this. | Q-007 |
| U-6 | Bank statement narration format, e.g. `NEFT-RAZORPAYSOFTWARE-UTR8837261-XYZ` | This is a *bank* artefact, not a Razorpay one. No gateway doc specifies it, and it varies per bank. Layer 4's regex-learning job exists precisely because this is unspecifiable. | Q-012 |
| U-7 | How an FX payment's INR credit is represented (rate, spread, which side rounds) | The docs confirm international cycles differ and that non-INR currencies have different sub-unit scales, but give no conversion representation. | Q-010 |
| U-8 | Whether a lost dispute's reversal on representment appears as a credit row or a status change | Only `amount_deducted` on `lost` is documented. | Q-011 |

## Sources fetched 2026-08-26

- `https://razorpay.com/docs/api/orders/entity`
- `https://razorpay.com/docs/api/orders/create/`
- `https://razorpay.com/docs/api/payments/entity`
- `https://razorpay.com/docs/api/refunds/entity`
- `https://razorpay.com/docs/api/settlements/entity`
- `https://razorpay.com/docs/api/settlements/fetch-recon/`
- `https://razorpay.com/docs/api/disputes/entity/`
- `https://razorpay.com/docs/payments/settlements/`
- `https://razorpay.com/docs/payments/settlements/dashboard/`

## Scope

**`transfer` rows are out of scope** (D-0012). They are a documented quarter of the recon
`type` enum — Route split-payment legs — but no fetched page defines their settlement
semantics, so generating them would mean measuring accuracy against a shape invented for the
occasion. The README states the gap. Do not add a `transfer` pathology without reopening Q-008.

## Test mode only

FinCtl makes **no** live gateway calls, in either direction, in any phase. There is no
Razorpay SDK in `requirements.txt` and no credential in `.env.example`. If that ever
changes it requires explicit approval first (ENGINEERING_RULES.md → "Stop and ask before"), and
test-mode credentials read from the environment — never a committed key.
