"""Tests for ingest and normalisation.

Weighted heavily toward the UTC/IST asymmetry, because that is the part with two distinct
failure directions and the part pathology 3 exists to catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.normalize import (
    IST,
    ist_date_covers,
    ist_date_of,
    load_bank_csv,
    load_dataset,
    load_merchant_csv,
    utc_window_of_ist_date,
)

UTC = timezone.utc


def epoch_ist(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=IST).timestamp())


# --- the interval rule --------------------------------------------------------------------


def test_ist_date_spans_1830z_to_1830z() -> None:
    """SPEC §3.4: IST date D covers [D-1 18:30:00Z, D 18:30:00Z)."""
    start, end = utc_window_of_ist_date("2026-03-31")

    assert datetime.fromtimestamp(start, tz=UTC) == datetime(2026, 3, 30, 18, 30, tzinfo=UTC)
    assert datetime.fromtimestamp(end, tz=UTC) == datetime(2026, 3, 31, 18, 30, tzinfo=UTC)
    assert end - start == 86_400


def test_the_window_is_half_open_so_the_boundary_belongs_to_one_day_only() -> None:
    """1830Z exactly belongs to the next IST day. A closed interval would put it in both."""
    boundary = int(datetime(2026, 3, 31, 18, 30, tzinfo=UTC).timestamp())

    assert not ist_date_covers("2026-03-31", boundary)
    assert ist_date_covers("2026-04-01", boundary)


def test_pathology_3_falls_inside_the_period_by_two_minutes() -> None:
    """23:58 IST on the last day is 18:28Z — inside the 18:30Z cutoff.

    A correct implementation includes it. This is the whole point of pathology 3.
    """
    late = epoch_ist(2026, 3, 31, 23, 58)

    assert datetime.fromtimestamp(late, tz=UTC) == datetime(2026, 3, 31, 18, 28, tzinfo=UTC)
    assert ist_date_of(late) == "2026-03-31"
    assert ist_date_covers("2026-03-31", late)


def test_the_off_by_one_that_a_utc_date_truncation_would_cause() -> None:
    """00:30 IST on 1 April is 19:00Z on 31 March.

    Truncating the UTC timestamp to its UTC date files it under March — pulling an April
    transaction into the closing period. This is the failure direction that inflates a
    month-end and is nearly invisible in aggregate.
    """
    early_april = epoch_ist(2026, 4, 1, 0, 30)
    as_utc = datetime.fromtimestamp(early_april, tz=UTC)

    assert as_utc == datetime(2026, 3, 31, 19, 0, tzinfo=UTC)
    assert as_utc.date().isoformat() == "2026-03-31", "the naive reading"
    assert ist_date_of(early_april) == "2026-04-01", "the correct reading"
    assert not ist_date_covers("2026-03-31", early_april)


@pytest.mark.parametrize(
    ("hour", "minute"),
    [(0, 0), (0, 30), (5, 29), (5, 30), (12, 0), (18, 29), (23, 58), (23, 59)],
)
def test_every_ist_hour_maps_back_to_its_own_date(hour: int, minute: int) -> None:
    """Round trip across the whole day, including the 00:00-05:29 band that breaks naive code."""
    instant = epoch_ist(2026, 3, 15, hour, minute)
    assert ist_date_of(instant) == "2026-03-15"
    assert ist_date_covers("2026-03-15", instant)


# --- coercion -----------------------------------------------------------------------------


def test_csv_empty_strings_become_none_not_the_string_none(tmp_path: Path) -> None:
    path = tmp_path / "merchant.csv"
    path.write_text(
        "row_id,kind,order_ref,gateway_order_id,amount_paise,currency,minor_unit_scale,"
        "issued_at_utc,customer_ref\n"
        "ml_1,order,receipt#1,,10000,INR,100,1772000000,\n",
        encoding="utf-8",
    )
    (row,) = load_merchant_csv(path)

    assert row.gateway_order_id is None
    assert row.customer_ref is None, "pathology 7 depends on this being None, not 'None'"
    assert row.amount_paise == 10000


def test_a_contradictory_minor_unit_scale_is_refused(tmp_path: Path) -> None:
    """A wrong scale mis-scales the amount by a power of ten, silently."""
    path = tmp_path / "merchant.csv"
    path.write_text(
        "row_id,kind,order_ref,gateway_order_id,amount_paise,currency,minor_unit_scale,"
        "issued_at_utc,customer_ref\n"
        "ml_1,order,receipt#1,,10000,INR,1,1772000000,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="contradicts"):
        load_merchant_csv(path)


def test_a_non_iso_value_date_is_refused(tmp_path: Path) -> None:
    """Bank rows carry no time, so the date is the only anchor there is."""
    path = tmp_path / "bank.csv"
    path.write_text(
        "row_id,value_date_ist,narration,reference,credit_paise,debit_paise,balance_paise\n"
        "bk_1,31/03/2026,NEFT,ref1,10000,0,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not an ISO date"):
        load_bank_csv(path)


def test_a_non_numeric_amount_is_refused_rather_than_coerced(tmp_path: Path) -> None:
    path = tmp_path / "bank.csv"
    path.write_text(
        "row_id,value_date_ist,narration,reference,credit_paise,debit_paise,balance_paise\n"
        "bk_1,2026-03-31,NEFT,ref1,1000.50,0,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not an integer"):
        load_bank_csv(path)


def test_duplicate_row_ids_across_sources_are_refused(tmp_path: Path) -> None:
    """Record identity is (source, row_id) and the ledger references records by id alone."""
    merchant = tmp_path / "m.csv"
    merchant.write_text(
        "row_id,kind,order_ref,gateway_order_id,amount_paise,currency,minor_unit_scale,"
        "issued_at_utc,customer_ref\nshared_id,order,r1,,10000,INR,100,1772000000,\n",
        encoding="utf-8",
    )
    gateway = tmp_path / "g.json"
    gateway.write_text("[]", encoding="utf-8")
    bank = tmp_path / "b.csv"
    bank.write_text(
        "row_id,value_date_ist,narration,reference,credit_paise,debit_paise,balance_paise\n"
        "shared_id,2026-03-31,NEFT,ref1,10000,0,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate row_id"):
        load_dataset(merchant=merchant, gateway=gateway, bank=bank)


# --- against the real datasets -------------------------------------------------------------


def test_the_real_datasets_normalise_cleanly() -> None:
    from data.generator import DATASET_SEEDS, dataset_paths

    for name in sorted(DATASET_SEEDS):
        paths = dataset_paths(name)
        if not paths["gateway"].exists():
            pytest.skip("run `make seed` first — data/generated/ is gitignored")
        data = load_dataset(
            merchant=paths["merchant"], gateway=paths["gateway"], bank=paths["bank"]
        )
        assert data.record_count > 400
        assert all(row.settlement_id for row in data.gateway_rows if row.settlement_utr), (
            "a UTR without a settlement id got through normalisation"
        )
