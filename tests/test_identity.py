"""Tests for Layer 1.

The behaviours that matter are the refusals: approving only at zero tolerance, distinguishing
absence from mismatch, and declining rather than guessing when a reference is ambiguous. A
Layer 1 that just joins would score better and mean less.
"""

from __future__ import annotations

from core import identity
from core.normalize import NormalizedDataset
from core.records import BankRow, GatewayRow, MerchantLedgerRow


def payment(
    row_id: str,
    credit: int,
    *,
    settlement_id: str | None = "setl_1",
    utr: str | None = "utr1",
    fee: int = 0,
    gst: int = 0,
    settled_at: int | None = 1_772_000_000,
    receipt: str | None = None,
) -> GatewayRow:
    return GatewayRow(
        row_id=row_id,
        type="payment",
        entity_id=f"pay_{row_id}",
        debit_paise=0,
        credit_paise=credit,
        fee_base_paise=fee,
        gst_paise=gst,
        currency="INR",
        created_at_utc=1_771_900_000,
        settled=settlement_id is not None,
        settlement_id=settlement_id,
        settlement_utr=utr if settlement_id else None,
        settled_at_utc=settled_at,
        order_receipt=receipt,
    )


def credit_row(row_id: str, amount: int, *, reference: str = "utr1", date: str = "2026-02-25") -> BankRow:
    return BankRow(
        row_id=row_id,
        value_date_ist=date,
        narration=f"NEFT-RAZORPAYSOFTWARE-UTR{reference}-STL",
        reference=reference,
        credit_paise=amount,
        debit_paise=0,
    )


def dataset(gateway=(), bank=(), merchant=()) -> NormalizedDataset:
    return NormalizedDataset(
        merchant_rows=list(merchant), gateway_rows=list(gateway), bank_rows=list(bank)
    )


def test_a_balancing_batch_is_approved() -> None:
    rows = [payment("gw_1", 10_000, fee=200, gst=36), payment("gw_2", 5_000, fee=100, gst=18)]
    expected = sum(r.net_paise for r in rows)

    result = identity.resolve(dataset(gateway=rows, bank=[credit_row("bk_1", expected)]))

    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.delta_paise == 0
    assert set(group.record_ids) == {"gw_1", "gw_2", "bk_1"}
    assert not result.exceptions


def test_a_batch_off_by_one_paisa_is_not_approved() -> None:
    """Zero tolerance. A non-zero tolerance is how false matches enter while coverage rises."""
    rows = [payment("gw_1", 10_000, fee=200, gst=36)]
    expected = sum(r.net_paise for r in rows)

    result = identity.resolve(dataset(gateway=rows, bank=[credit_row("bk_1", expected + 1)]))

    assert not result.groups, "a 1-paisa discrepancy was approved"
    assert len(result.candidates) == 1
    assert result.candidates[0].delta_paise == 1


def test_a_batch_with_no_bank_credit_is_absence_not_a_search() -> None:
    """SPEC §4.1 M0: no scalar to reconstruct against, so nothing to search for."""
    result = identity.resolve(dataset(gateway=[payment("gw_1", 10_000)], bank=[]))

    assert not result.candidates, "a missing credit was handed to Layer 2 as if searchable"
    assert [e.exception_type for e in result.exceptions] == [identity.EX_MISSING_BANK_ROW]


def test_a_bank_credit_with_no_batch_is_reported() -> None:
    result = identity.resolve(dataset(gateway=[], bank=[credit_row("bk_1", 10_000)]))

    assert [e.exception_type for e in result.exceptions] == [identity.EX_MISSING_GATEWAY_ROW]


def test_a_duplicate_reference_is_disambiguated_by_value_date() -> None:
    """Pathology 2, resolvable: an exact composite key of reference plus IST value date.

    The two batches settle on 2026-02-25 and 2026-03-02 IST, so the value date picks
    them apart without any fuzzy matching.
    """
    rows_a = [payment("gw_1", 10_000, settlement_id="setl_a", utr="shared")]
    rows_b = [payment("gw_2", 20_000, settlement_id="setl_b", utr="shared", settled_at=1_772_400_000)]

    result = identity.resolve(
        dataset(
            gateway=rows_a + rows_b,
            bank=[
                credit_row("bk_a", 10_000, reference="shared", date="2026-02-25"),
                credit_row("bk_b", 20_000, reference="shared", date="2026-03-02"),
            ],
        )
    )

    assert len(result.groups) == 2, [e.detail for e in result.exceptions]
    assert not result.exceptions


def test_a_duplicate_reference_with_colliding_dates_is_refused() -> None:
    """Pathology 2, unresolvable: nothing distinguishes the two, so it refuses."""
    rows_a = [payment("gw_1", 10_000, settlement_id="setl_a", utr="shared")]
    rows_b = [payment("gw_2", 20_000, settlement_id="setl_b", utr="shared")]

    result = identity.resolve(
        dataset(
            gateway=rows_a + rows_b,
            bank=[
                credit_row("bk_a", 10_000, reference="shared", date="2026-02-25"),
                credit_row("bk_b", 20_000, reference="shared", date="2026-02-25"),
            ],
        )
    )

    assert not result.groups, "picked one of two indistinguishable credits"
    types = {e.exception_type for e in result.exceptions}
    assert types == {identity.EX_DUPLICATE_REFERENCE}


def test_pool_rows_are_reported_as_the_search_space_not_matched() -> None:
    """A row with no settlement_id is legitimately unassigned, not missing data."""
    rows = [payment("gw_1", 10_000), payment("gw_pool", 5_000, settlement_id=None)]
    expected = sum(r.net_paise for r in rows if r.settlement_id)

    result = identity.resolve(dataset(gateway=rows, bank=[credit_row("bk_1", expected)]))

    assert result.pool_row_ids == ["gw_pool"]
    assert "gw_pool" not in result.matched_record_ids


def test_merchant_rows_join_by_order_receipt() -> None:
    row = payment("gw_1", 10_000, receipt="receipt#7")
    merchant = MerchantLedgerRow(
        row_id="ml_1",
        kind="order",
        order_ref="receipt#7",
        gateway_order_id=None,
        amount_paise=10_000,
        currency="INR",
        minor_unit_scale=100,
        issued_at_utc=1_771_900_000,
        customer_ref="cust_1",
    )

    result = identity.resolve(
        dataset(gateway=[row], bank=[credit_row("bk_1", row.net_paise)], merchant=[merchant])
    )

    assert set(result.groups[0].record_ids) == {"gw_1", "bk_1", "ml_1"}


def test_unresolved_records_are_never_silently_dropped() -> None:
    """The cascade contract: a later layer gets exactly what earlier ones did not settle."""
    rows = [payment("gw_1", 10_000), payment("gw_pool", 5_000, settlement_id=None)]
    data = dataset(gateway=rows, bank=[credit_row("bk_1", rows[0].net_paise)])

    result = identity.resolve(data)
    leftover = identity.unresolved_record_ids(data, result)

    accounted = set(result.matched_record_ids) | set(leftover)
    for exception in result.exceptions:
        accounted |= set(exception.record_ids)
    assert accounted == {"gw_1", "gw_pool", "bk_1"}


def test_layer_1_is_deterministic() -> None:
    rows = [payment(f"gw_{i}", 1_000 * i, fee=i, gst=i) for i in range(1, 8)]
    data = dataset(gateway=rows, bank=[credit_row("bk_1", sum(r.net_paise for r in rows))])

    first, second = identity.resolve(data), identity.resolve(data)
    assert [g.record_ids for g in first.groups] == [g.record_ids for g in second.groups]
    assert [e.record_ids for e in first.exceptions] == [e.record_ids for e in second.exceptions]
