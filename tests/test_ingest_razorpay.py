"""The ingestion adapter — D-0027.

The centre of gravity here is `fee_split`, because it is the single place the Q-002 assumption
lives and the only thing in the adapter that can be *wrong* rather than merely absent. Everything
else is a field rename, which a type error would catch; the fee split is arithmetic that would
balance plausibly while being off by exactly the GST.

No test in this file makes a network call. `fetch_recon` is exercised against a fake opener, so
the request FinCtl would build is asserted without one leaving the machine.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from core.ingest import razorpay
from core.ingest.razorpay import (
    AdapterError,
    ReconFetchError,
    adapt_recon_report,
    fee_split,
    fetch_recon,
    key_mode_warning,
    to_gateway_row,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _row(**overrides: Any) -> dict[str, Any]:
    """A recon-report row shaped exactly as the documented schema describes it."""
    base: dict[str, Any] = {
        "entity_id": "pay_NHmXvLmZlkFqPO",
        "type": "payment",
        "debit": 0,
        "credit": 140254_00,
        "amount": 140254_00,
        "currency": "INR",
        "fee": 3310_00,
        "tax": 505_00,
        "on_hold": False,
        "settled": True,
        "created_at": 1773100000,
        "settled_at": 1773300000,
        "settlement_id": "setl_7IZKKI4Pnt2kEe",
        "settlement_utr": "1597813219e1pq6w",
        "payment_id": None,
        "order_id": "order_RB58MiP5SPFYyM",
        "order_receipt": "rcpt_0001",
        "method": "card",
        "dispute_id": None,
    }
    base.update(overrides)
    return base


# --- the Q-002 conversion -----------------------------------------------------------------


def test_fee_split_treats_fee_as_gst_inclusive() -> None:
    """The whole of Q-002 in one assertion: base + gst == the reported fee."""
    fee_base, gst = fee_split(3310_00, 505_00)

    assert fee_base == 2805_00
    assert gst == 505_00
    assert fee_base + gst == 3310_00, "the split must reconstruct the reported fee exactly"


def test_fee_split_does_not_double_count_gst() -> None:
    """The failure this function exists to prevent, stated as the wrong answer it must not give.

    Subtracting `fee` and `tax` both from the gross — the other reading of the documentation —
    removes the GST twice. On this row that is Rs 505 of error on a single transaction, which is
    small enough to look like a rounding bug and large enough to fail every balance check.
    """
    fee, tax = 3310_00, 505_00
    fee_base, gst = fee_split(fee, tax)

    double_counted = fee + tax
    assert fee_base + gst != double_counted
    assert double_counted - (fee_base + gst) == tax


def test_fee_split_is_integer_only() -> None:
    """No float may appear on the money path, adapter included (invariant 1)."""
    fee_base, gst = fee_split(1, 1)
    assert isinstance(fee_base, int) and isinstance(gst, int)
    assert not isinstance(fee_base, bool)


def test_fee_split_refuses_a_tax_larger_than_its_fee() -> None:
    """A negative fee base means the inclusive reading is wrong for that row — a finding.

    Clamping it to zero would erase exactly the evidence this adapter was built to collect.
    """
    with pytest.raises(AdapterError, match="cannot be GST-inclusive"):
        fee_split(100, 101)


def test_fee_split_lives_in_exactly_one_place() -> None:
    """One audited site, so there is one thing to correct when a real report settles Q-002.

    Asserted over the parsed syntax tree of the whole package, not by counting a string. The
    first version of this test counted occurrences of `"fee - tax"` in the source and failed on
    the docstring that *documents* the conversion — a check measuring a stand-in for the property
    rather than the property, which is the failure this repository keeps logging. What matters is
    that no other function computes the subtraction, so that is what is checked.
    """
    import ast

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.BinOp)
                    and isinstance(inner.op, ast.Sub)
                    and isinstance(inner.left, ast.Name)
                    and isinstance(inner.right, ast.Name)
                    and {inner.left.id, inner.right.id} == {"fee", "tax"}
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}::{node.name}")

    assert offenders == ["core/ingest/razorpay.py::fee_split"], (
        f"the Q-002 conversion is computed in more than one place: {offenders}"
    )


def test_a_zero_fee_row_splits_to_zero() -> None:
    """Documented: for a normal settlement the settlement-level fee and tax are 0."""
    assert fee_split(0, 0) == (0, 0)


# --- row mapping --------------------------------------------------------------------------


def test_a_payment_row_maps_onto_the_canonical_schema() -> None:
    row = to_gateway_row(_row())

    assert row.row_id == "pay_NHmXvLmZlkFqPO"
    assert row.entity_id == "pay_NHmXvLmZlkFqPO"
    assert row.type == "payment"
    assert row.credit_paise == 140254_00
    assert row.debit_paise == 0
    assert row.fee_base_paise == 2805_00
    assert row.gst_paise == 505_00
    assert row.settlement_utr == "1597813219e1pq6w"
    assert row.settled_at_utc == 1773300000


def test_the_net_contribution_uses_the_split_not_the_reported_fee() -> None:
    """`net_paise` subtracts base and GST separately, so the split must reconstruct the fee.

    If the adapter put the GST-inclusive `fee` into `fee_base_paise` and left `gst_paise` at the
    tax as well, this row's contribution would be short by exactly the GST — and the batch would
    fail to balance while looking like a data problem.
    """
    row = to_gateway_row(_row())
    assert row.net_paise == 140254_00 - 3310_00


def test_a_transfer_row_is_refused_by_name() -> None:
    """`transfer` is a documented member of the enum and deliberately out of scope (D-0012)."""
    with pytest.raises(AdapterError, match="out of scope"):
        to_gateway_row(_row(type="transfer"))


def test_an_undocumented_type_is_refused() -> None:
    with pytest.raises(AdapterError, match="not in the documented recon enum"):
        to_gateway_row(_row(type="chargeback"))


def test_a_row_without_an_entity_id_is_refused() -> None:
    with pytest.raises(AdapterError, match="entity_id is required"):
        to_gateway_row(_row(entity_id=None))


def test_international_is_not_inferred_from_currency() -> None:
    """A non-INR row is not necessarily an international card, and guessing would hide that."""
    row = to_gateway_row(_row(currency="USD"))
    assert row.international is False


def test_skipped_rows_are_reported_not_dropped() -> None:
    """A count that silently excludes what it could not read is the undisclosed-subset failure."""
    report = adapt_recon_report([_row(), _row(entity_id="tr_1", type="transfer")])

    assert len(report.rows) == 1
    assert len(report.skipped) == 1
    assert report.skipped[0][0] == "tr_1"
    assert report.total_seen == 2


def test_one_settlement_at_a_time_is_enforced_in_the_data() -> None:
    items = [
        _row(entity_id="pay_1", settlement_id="setl_A"),
        _row(entity_id="pay_2", settlement_id="setl_B"),
    ]
    report = adapt_recon_report(items, settlement_id="setl_A")

    assert [r.row_id for r in report.rows] == ["pay_1"]
    assert report.settlement_ids == ["setl_A", "setl_B"], "the others are still named"


# --- the request, without making one ------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_the_request_is_a_get_with_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserts the request FinCtl builds. Nothing leaves the machine."""
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int = 0) -> _FakeResponse:
        seen["method"] = request.method
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        return _FakeResponse({"entity": "collection", "count": 1, "items": [_row()]})

    monkeypatch.setattr(razorpay.urllib.request, "urlopen", fake_urlopen)

    items = fetch_recon(
        key_id="rzp_test_abc", key_secret="s3cret", year=2026, month=3, day=27, count=50
    )

    assert seen["method"] == "GET", "the adapter must only ever read"
    assert seen["url"].startswith(razorpay.RECON_ENDPOINT)
    assert "year=2026" in seen["url"] and "month=3" in seen["url"] and "day=27" in seen["url"]

    expected = base64.b64encode(b"rzp_test_abc:s3cret").decode()
    assert seen["headers"]["Authorization"] == f"Basic {expected}"
    assert len(items) == 1


def test_only_the_get_verb_appears_in_the_module() -> None:
    """Read-only is structural: there is no write path to disable, because none was written."""
    source = (REPO_ROOT / "core" / "ingest" / "razorpay.py").read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert not re.search(rf'"{verb}"|\'{verb}\'', source), f"{verb} appears in the adapter"


def test_a_missing_items_array_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        razorpay.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeResponse({"entity": "collection"}),
    )
    with pytest.raises(ReconFetchError, match="no `items` array"):
        fetch_recon(key_id="k", key_secret="s", year=2026, month=3)


def test_parameters_are_validated_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad month must not become a request; validation is not the server's job here."""
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a request was made despite invalid parameters")

    monkeypatch.setattr(razorpay.urllib.request, "urlopen", explode)

    with pytest.raises(ReconFetchError, match="month must be"):
        fetch_recon(key_id="k", key_secret="s", year=2026, month=13)
    with pytest.raises(ReconFetchError, match="count must be"):
        fetch_recon(key_id="k", key_secret="s", year=2026, month=3, count=5000)
    with pytest.raises(ReconFetchError, match="key id and a key secret are required"):
        fetch_recon(key_id="", key_secret="s", year=2026, month=3)


# --- test mode ----------------------------------------------------------------------------


def test_a_non_test_key_warns_rather_than_refusing() -> None:
    """The prefix convention is UNVERIFIED (Q-016), so it cannot be enforced as a rule."""
    assert key_mode_warning("rzp_test_abc") is None

    warning = key_mode_warning("rzp_live_abc")
    assert warning is not None
    assert "unverified" in warning.lower()
    assert "GET" in warning, "the warning should say what actually protects the account"
