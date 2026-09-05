"""Map a real settlement recon report onto the canonical schema. Read-only, test mode (D-0027).

**What this is for.** Every accuracy figure this project reports is measured on synthetic data.
A real gateway connection cannot improve those figures and must never be allowed to look as
though it did. What it *can* do is settle **Q-002** — whether the recon report's `fee` is
inclusive of its `tax` — which is the one open domain question that changes the netting
arithmetic. So this module exists to read one real settlement and show what the canonical
records would be, and for nothing else.

**Read-only is structural.** `_get_json` hard-codes the HTTP method and raises on anything else.
There is no create, update or delete path in this module to disable, because none was written.

**Field names come from fetched documentation**, never from memory — see
`skills/razorpay-domain/SKILL.md` for the recon-report row schema and D-0027 for the endpoint
and auth scheme, each with the page it was quoted from.

Money stays integer minor units throughout, exactly as it does everywhere else: the recon report
already speaks in subunits ("The amount, in currency subunits, that has been debited"), so there
is no parsing step here that could introduce a float.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.money import Paise
from core.records import GatewayRow

__all__ = [
    "RECON_ENDPOINT",
    "TEST_KEY_PREFIX",
    "AdapterError",
    "ReconFetchError",
    "adapt_recon_report",
    "fee_split",
    "fetch_recon",
    "key_mode_warning",
    "to_gateway_row",
]

# Verified 2026-09-05 from https://razorpay.com/docs/api/settlements/fetch-recon/
RECON_ENDPOINT = "https://api.razorpay.com/v1/settlements/recon/combined"

# UNVERIFIED (Q-016): the documentation does not state that a test-mode key id carries a
# distinguishing prefix. This constant therefore drives a *warning*, never a guarantee, and the
# real safety property is that this module has no write path at all.
TEST_KEY_PREFIX = "rzp_test_"

# `transfer` is a documented member of the recon `type` enum and is deliberately out of scope
# (D-0012, Q-008): no fetched page defines its settlement semantics, so adapting one would mean
# inventing a shape. Skipped explicitly and counted, never silently dropped.
SUPPORTED_TYPES = frozenset({"payment", "refund", "adjustment"})
OUT_OF_SCOPE_TYPES = frozenset({"transfer"})


class AdapterError(ValueError):
    """A recon row could not be mapped onto the canonical schema."""


class ReconFetchError(RuntimeError):
    """The recon report could not be fetched. Never carries the credential (item 5)."""


def fee_split(fee: int, tax: int) -> tuple[Paise, Paise]:
    """Split the reported fee into its GST-exclusive base and its GST. **The Q-002 conversion.**

    This is the single audited place where that assumption lives, so there is one site to test
    and one site to correct when a real report settles the question.

        fee_base = fee - tax        (`fee` is GST-INCLUSIVE)
        gst      = tax

    **Why this reading.** The Payment entity documents `fee` as *"Fee (including GST) charged by
    Razorpay"* and `tax` as *"GST charged for the payment"*. The dashboard settlement break-up
    reads `Payment - Adjustment - Tax - Fee`, which points the other way — subtracting both. The
    two differ by exactly the GST, which is small enough to look like a rounding bug and large
    enough to fail every balance check.

    FinCtl's canonical schema stores `fee_base_paise` and `gst_paise` separately (D-0003)
    precisely so that it is correct under *either* reading; this function is where the wire
    format's ambiguity is resolved into that schema, and it resolves it the inclusive way.

    Raises rather than clamping when `tax > fee`. A negative fee base is not a rounding artefact
    — it means the inclusive reading is wrong for this row, which is exactly the finding this
    adapter exists to surface, and silently flooring it at zero would erase it.
    """
    if fee < 0:
        raise AdapterError(f"fee must be non-negative, got {fee}")
    if tax < 0:
        raise AdapterError(f"tax must be non-negative, got {tax}")
    if tax > fee:
        raise AdapterError(
            f"tax {tax} exceeds fee {fee}, so `fee` cannot be GST-inclusive for this row. "
            "That is a finding about Q-002, not a rounding error: see docs/OPEN_QUESTIONS.md."
        )
    return fee - tax, tax


def _as_int(row: dict[str, Any], field: str, *, default: int | None = 0) -> int:
    value = row.get(field, default)
    if value is None:
        if default is None:
            raise AdapterError(f"{field} is required and absent")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterError(f"{field}={value!r} is not an integer; the recon report sends integers")
    return value


def _as_optional_str(row: dict[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    text = str(value)
    return text or None


def to_gateway_row(item: dict[str, Any]) -> GatewayRow:
    """One recon-report row into one canonical `GatewayRow`.

    `entity_id` becomes `row_id`: it is documented as *"The unique identifier of the transaction
    that has been settled"*, so it is already the stable per-row key, and inventing a synthetic id
    would break the audit ledger's ability to name a real record.

    `international` has no counterpart in the recon report, so it stays at its schema default
    rather than being guessed from `currency` — a non-INR row is not necessarily an international
    card, and encoding that inference here would put an assumption in an adapter where it cannot
    be seen.
    """
    kind = str(item.get("type") or "")
    if kind in OUT_OF_SCOPE_TYPES:
        raise AdapterError(
            f"type {kind!r} is out of scope (D-0012): no fetched page defines its settlement "
            "semantics, so adapting it would mean inventing one."
        )
    if kind not in SUPPORTED_TYPES:
        raise AdapterError(
            f"type {kind!r} is not in the documented recon enum {sorted(SUPPORTED_TYPES)}"
        )

    entity_id = _as_optional_str(item, "entity_id")
    if not entity_id:
        raise AdapterError("entity_id is required; it is the row's identity")

    fee_base, gst = fee_split(_as_int(item, "fee"), _as_int(item, "tax"))

    return GatewayRow(
        row_id=entity_id,
        type=kind,  # type: ignore[arg-type]
        entity_id=entity_id,
        debit_paise=_as_int(item, "debit"),
        credit_paise=_as_int(item, "credit"),
        fee_base_paise=fee_base,
        gst_paise=gst,
        currency=str(item.get("currency") or "INR"),
        created_at_utc=_as_int(item, "created_at", default=None),
        on_hold=bool(item.get("on_hold", False)),
        settled=bool(item.get("settled", False)),
        payment_id=_as_optional_str(item, "payment_id"),
        order_id=_as_optional_str(item, "order_id"),
        order_receipt=_as_optional_str(item, "order_receipt"),
        settlement_id=_as_optional_str(item, "settlement_id"),
        settlement_utr=_as_optional_str(item, "settlement_utr"),
        settled_at_utc=(
            _as_int(item, "settled_at", default=0) if item.get("settled_at") else None
        ),
        dispute_id=_as_optional_str(item, "dispute_id"),
        method=_as_optional_str(item, "method"),
    )


@dataclass(frozen=True, slots=True)
class AdaptedReport:
    """What one recon report produced, including what it could not.

    Skipped rows are counted and named rather than dropped: a page reporting "12 records" from a
    report of 20 while saying nothing about the other 8 is the same class of dishonesty as a
    coverage rate computed over an undisclosed subset.
    """

    rows: list[GatewayRow]
    skipped: list[tuple[str, str]]
    settlement_ids: list[str]

    @property
    def total_seen(self) -> int:
        return len(self.rows) + len(self.skipped)


def adapt_recon_report(
    items: list[dict[str, Any]], *, settlement_id: str | None = None
) -> AdaptedReport:
    """Adapt every row, optionally narrowing to one settlement.

    `settlement_id` is how "one settlement at a time" is enforced in the data rather than in the
    UI: the report is fetched for a month, and only the named settlement's rows are adapted.
    """
    rows: list[GatewayRow] = []
    skipped: list[tuple[str, str]] = []
    seen: list[str] = []

    for item in items:
        this_settlement = str(item.get("settlement_id") or "")
        if this_settlement and this_settlement not in seen:
            seen.append(this_settlement)
        if settlement_id and this_settlement != settlement_id:
            continue
        try:
            rows.append(to_gateway_row(item))
        except (AdapterError, ValueError) as error:
            skipped.append((str(item.get("entity_id") or "(no entity_id)"), str(error)))

    return AdaptedReport(rows=rows, skipped=skipped, settlement_ids=sorted(seen))


def key_mode_warning(key_id: str) -> str | None:
    """A warning when the key id does not look like a test-mode key, or `None`.

    Deliberately a warning and not a refusal: the documentation does not state that test-mode key
    ids carry a distinguishing prefix (Q-016), so refusing on this basis would enforce a rule the
    gateway never published. The guarantee that matters is elsewhere — this module has no write
    path.
    """
    if key_id.startswith(TEST_KEY_PREFIX):
        return None
    return (
        f"This key id does not begin {TEST_KEY_PREFIX!r}, so it may not be a test-mode key. "
        "FinCtl only ever issues GET requests and cannot modify anything through this key, but "
        "test-mode credentials are the documented expectation. The prefix convention itself is "
        "unverified (Q-016)."
    )


def _get_json(url: str, *, key_id: str, key_secret: str, timeout: int = 20) -> dict[str, Any]:
    """The only network function in this module, and it can only ever read.

    The method is a literal, so there is no parameter through which a caller could turn this into
    a write. `test_secrets.py` asserts that no other HTTP verb appears anywhere in this file.

    **The credential never reaches an error message.** `urllib` puts the request URL into the
    exception it raises, and the header is not in the URL — but the failure text is rewritten
    here regardless, so that no future change to how the request is built can start leaking one
    through a traceback.
    """
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode("ascii")
    request = urllib.request.Request(
        url,
        method="GET",  # read-only by construction (D-0027)
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "finctl/0.6 (+read-only settlement recon adapter)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise ReconFetchError(
            f"the gateway refused the request with HTTP {error.code}. "
            "Check that the key id and secret are a matching test-mode pair."
        ) from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise ReconFetchError(f"could not reach the gateway: {error.reason}") from None
    except json.JSONDecodeError:
        raise ReconFetchError("the gateway returned a response that is not JSON") from None

    if not isinstance(payload, dict):
        raise ReconFetchError("expected a JSON object envelope from the recon endpoint")
    return payload


def fetch_recon(
    *,
    key_id: str,
    key_secret: str,
    year: int,
    month: int,
    day: int | None = None,
    count: int = 100,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """Fetch one month (or day) of the settlement recon report. **Test mode. Read-only.**

    Endpoint, auth scheme and parameter names were read from the fetched documentation on
    2026-09-05 and are recorded in D-0027 with their source pages. `count` is capped by the API
    at 1000; the default here is deliberately far below that, because this reads *one settlement*
    for inspection and not a bulk export.

    Returns the `items` array. Raises `ReconFetchError` on any failure, with a message that never
    contains the credential.
    """
    if not key_id or not key_secret:
        raise ReconFetchError("both a key id and a key secret are required")
    if not 1 <= month <= 12:
        raise ReconFetchError(f"month must be 1..12, got {month}")
    if day is not None and not 1 <= day <= 31:
        raise ReconFetchError(f"day must be 1..31, got {day}")
    if not 1 <= count <= 1000:
        raise ReconFetchError(f"count must be 1..1000 per the API, got {count}")

    query = f"?year={int(year)}&month={int(month)}&count={int(count)}"
    if day is not None:
        query += f"&day={int(day)}"

    payload = _get_json(
        RECON_ENDPOINT + query, key_id=key_id, key_secret=key_secret, timeout=timeout
    )
    items = payload.get("items")
    if not isinstance(items, list):
        raise ReconFetchError(
            "the recon response has no `items` array; the documented envelope is "
            '{"entity":"collection","count":N,"items":[...]}'
        )
    return items
