"""Ingest and normalisation — raw source rows into canonical records.

Three jobs, in order of how much trouble they cause:

1. **The UTC/IST asymmetry.** Gateway timestamps are epoch UTC integers; bank statements are
   IST dates with no time at all. Reconciling them needs an explicit interval rule, and both
   directions of getting it wrong are real (`docs/SPEC.md` §3.4). This is the part that earns
   the module.
2. **Type coercion.** CSV gives every field as a string, including `""` for `None` and
   `"True"` for a bool. JSON gives native types. Both arrive here and leave as typed records.
3. **Currency normalisation.** Non-INR amounts carry their own minor-unit scale, so "paise" is
   shorthand for "integer minor units" and the scale is a property of the currency.

Reads files by path and imports nothing from `data/` or `eval/` — invariant 2. Path
resolution is a caller's job, so this module never learns the dataset naming convention.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.money import Paise, minor_unit_scale
from core.records import BankRow, GatewayRow, MerchantLedgerRow

__all__ = [
    "IST",
    "NormalizedDataset",
    "ist_date_covers",
    "ist_date_of",
    "load_bank_csv",
    "load_dataset",
    "load_gateway_json",
    "load_merchant_csv",
    "utc_window_of_ist_date",
]

IST = timezone(timedelta(hours=5, minutes=30))
"""India Standard Time: UTC+05:30, no DST. The offset never changes, which is the one mercy
in this part of the domain."""


@dataclass(frozen=True, slots=True)
class NormalizedDataset:
    """The three sources, typed. No ground truth — that lives in a separate file `core`
    cannot reach."""

    merchant_rows: list[MerchantLedgerRow]
    gateway_rows: list[GatewayRow]
    bank_rows: list[BankRow]

    @property
    def record_count(self) -> int:
        return len(self.merchant_rows) + len(self.gateway_rows) + len(self.bank_rows)


# --- the timezone rule -------------------------------------------------------------------


def utc_window_of_ist_date(value_date_ist: str) -> tuple[int, int]:
    """The half-open UTC interval an IST value date covers: `[start, end)`.

    IST date `D` runs from `D−1 18:30:00Z` to `D 18:30:00Z`. Both ways of getting this wrong
    show up in real reconciliations:

    * Truncating a UTC timestamp to its UTC date files everything from `18:30Z` to midnight
      under the *previous* IST day — that is every transaction between 00:00 and 05:29 IST.
    * Writing a period cutoff as `…T23:59:59Z` instead of `…T18:30:00Z` pulls the next IST
      day's early hours into the closing period.

    Half-open on purpose: `18:30:00Z` exactly belongs to the *next* day, and a closed interval
    would put it in both.
    """
    day = date.fromisoformat(value_date_ist)
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def ist_date_of(epoch_utc: int) -> str:
    """The IST calendar date a UTC instant falls on, as `YYYY-MM-DD`.

    `23:58 IST` on 2026-03-31 is `18:28Z` the same day, two minutes inside the cutoff, and
    must come back as `2026-03-31`. `00:30 IST` on 2026-04-01 is `19:00Z` on **March 31**, and
    must come back as `2026-04-01` — that pair is pathology 3.
    """
    return datetime.fromtimestamp(epoch_utc, tz=IST).date().isoformat()


def ist_date_covers(value_date_ist: str, epoch_utc: int) -> bool:
    """Whether a UTC instant falls on the given IST value date."""
    start, end = utc_window_of_ist_date(value_date_ist)
    return start <= epoch_utc < end


# --- coercion ----------------------------------------------------------------------------

_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no", ""})


def _require(row: dict[str, Any], field: str, context: str) -> Any:
    if field not in row:
        raise ValueError(f"{context}: missing required field {field!r}")
    return row[field]


def _as_int(value: Any, *, field: str, context: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{context}: {field} is a bool where an integer was expected")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError(f"{context}: {field} is empty where an integer was expected")
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{context}: {field}={value!r} is not an integer") from None


def _as_optional_int(value: Any, *, field: str, context: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _as_int(value, field=field, context=context)


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text and text != "None" else None


def _as_bool(value: Any, *, field: str, context: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{context}: {field}={value!r} is not a boolean")


# --- loaders ------------------------------------------------------------------------------


def load_merchant_csv(path: Path) -> list[MerchantLedgerRow]:
    rows: list[MerchantLedgerRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for line_number, raw in enumerate(csv.DictReader(handle), start=2):
            context = f"{path.name}:{line_number}"
            currency = str(_require(raw, "currency", context))
            declared = _as_optional_int(raw.get("minor_unit_scale"), field="minor_unit_scale", context=context)
            resolved = minor_unit_scale(currency)
            if declared is not None and declared != resolved:
                raise ValueError(
                    f"{context}: minor_unit_scale={declared} contradicts {currency} "
                    f"(={resolved}). A wrong scale silently mis-scales the amount by a power "
                    "of ten, so it is refused rather than trusted."
                )
            rows.append(
                MerchantLedgerRow(
                    row_id=str(_require(raw, "row_id", context)),
                    kind=str(_require(raw, "kind", context)),  # type: ignore[arg-type]
                    order_ref=str(_require(raw, "order_ref", context)),
                    gateway_order_id=_as_optional_str(raw.get("gateway_order_id")),
                    amount_paise=_as_int(_require(raw, "amount_paise", context), field="amount_paise", context=context),
                    currency=currency,
                    minor_unit_scale=resolved,
                    issued_at_utc=_as_int(_require(raw, "issued_at_utc", context), field="issued_at_utc", context=context),
                    customer_ref=_as_optional_str(raw.get("customer_ref")),
                )
            )
    return rows


def load_gateway_json(path: Path) -> list[GatewayRow]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path.name}: expected a JSON array of gateway rows")

    rows: list[GatewayRow] = []
    for index, raw in enumerate(payload):
        context = f"{path.name}[{index}]"
        rows.append(
            GatewayRow(
                row_id=str(_require(raw, "row_id", context)),
                type=str(_require(raw, "type", context)),  # type: ignore[arg-type]
                entity_id=str(_require(raw, "entity_id", context)),
                debit_paise=_as_int(_require(raw, "debit_paise", context), field="debit_paise", context=context),
                credit_paise=_as_int(_require(raw, "credit_paise", context), field="credit_paise", context=context),
                fee_base_paise=_as_int(_require(raw, "fee_base_paise", context), field="fee_base_paise", context=context),
                gst_paise=_as_int(_require(raw, "gst_paise", context), field="gst_paise", context=context),
                currency=str(_require(raw, "currency", context)),
                created_at_utc=_as_int(_require(raw, "created_at_utc", context), field="created_at_utc", context=context),
                on_hold=_as_bool(raw.get("on_hold", False), field="on_hold", context=context),
                settled=_as_bool(raw.get("settled", False), field="settled", context=context),
                payment_id=_as_optional_str(raw.get("payment_id")),
                order_id=_as_optional_str(raw.get("order_id")),
                order_receipt=_as_optional_str(raw.get("order_receipt")),
                settlement_id=_as_optional_str(raw.get("settlement_id")),
                settlement_utr=_as_optional_str(raw.get("settlement_utr")),
                settled_at_utc=_as_optional_int(raw.get("settled_at_utc"), field="settled_at_utc", context=context),
                dispute_id=_as_optional_str(raw.get("dispute_id")),
                method=_as_optional_str(raw.get("method")),
                international=_as_bool(raw.get("international", False), field="international", context=context),
                amount_minor_original=_as_optional_int(raw.get("amount_minor_original"), field="amount_minor_original", context=context),
                currency_original=_as_optional_str(raw.get("currency_original")),
                fx_rate_micros=_as_optional_int(raw.get("fx_rate_micros"), field="fx_rate_micros", context=context),
            )
        )
    return rows


def load_bank_csv(path: Path) -> list[BankRow]:
    rows: list[BankRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for line_number, raw in enumerate(csv.DictReader(handle), start=2):
            context = f"{path.name}:{line_number}"
            value_date = str(_require(raw, "value_date_ist", context))
            try:
                date.fromisoformat(value_date)
            except ValueError:
                raise ValueError(
                    f"{context}: value_date_ist={value_date!r} is not an ISO date. Bank value "
                    "dates carry no time, so the date itself is the only anchor available."
                ) from None
            rows.append(
                BankRow(
                    row_id=str(_require(raw, "row_id", context)),
                    value_date_ist=value_date,
                    narration=str(raw.get("narration") or ""),
                    reference=str(raw.get("reference") or ""),
                    credit_paise=_as_int(_require(raw, "credit_paise", context), field="credit_paise", context=context),
                    debit_paise=_as_int(_require(raw, "debit_paise", context), field="debit_paise", context=context),
                    balance_paise=_as_optional_int(raw.get("balance_paise"), field="balance_paise", context=context),
                )
            )
    return rows


def load_dataset(*, merchant: Path, gateway: Path, bank: Path) -> NormalizedDataset:
    """Load and normalise all three sources.

    Row ids must be globally unique across sources, because record identity is `(source,
    row_id)` and the audit ledger references records by id alone.
    """
    dataset = NormalizedDataset(
        merchant_rows=load_merchant_csv(merchant),
        gateway_rows=load_gateway_json(gateway),
        bank_rows=load_bank_csv(bank),
    )

    seen: dict[str, str] = {}
    for source, rows in (
        ("merchant", dataset.merchant_rows),
        ("gateway", dataset.gateway_rows),
        ("bank", dataset.bank_rows),
    ):
        for row in rows:
            if row.row_id in seen:
                raise ValueError(
                    f"duplicate row_id {row.row_id!r} in {source} and {seen[row.row_id]}; "
                    "record identity would be ambiguous and the audit ledger unreadable"
                )
            seen[row.row_id] = source

    return dataset


def expected_credit_paise(rows: list[GatewayRow]) -> Paise:
    """The recon-row form of the settlement identity (`SPEC.md` §4).

    `Σ credit − Σ debit − Σ fee_base − Σ gst`, in integer paise.

    `Σ gst` is a sum of stored per-row values, never a recomputation from the summed fee base:
    half-up rounding does not distribute over addition, so recomputing loses up to a paisa per
    row and the batch stops balancing while looking like a data problem.
    """
    return sum(row.net_paise for row in rows)
