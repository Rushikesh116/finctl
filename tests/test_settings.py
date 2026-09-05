"""The `/settings` page — D-0027.

Four properties, in descending order of how much damage getting them wrong would do:

1. `DEMO_MODE=1` disables it outright, and there is no path that accepts a key.
2. The static export draws no form, because there is nothing behind it to receive one.
3. Without keys the page still explains itself rather than erroring.
4. It never writes to the gateway, and never persists anything.

Nothing here makes a network call: the adapter's fetch is replaced with a fake so the endpoint's
behaviour is asserted without one leaving the machine.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.render_report import STATIC_LINKS, build_html


def _run() -> dict[str, Any]:
    """The minimum a page needs. The settings page renders no run figures of its own."""
    from tests.test_report import _run as base_run

    return base_run()


# --- the states ---------------------------------------------------------------------------


def test_the_static_export_draws_no_form() -> None:
    """There is no server behind an exported file, and a form posting nowhere is a dead control."""
    page = build_html(_run(), page="settings", links=STATIC_LINKS)

    assert "<form" not in page
    assert "Not available on this page" in page
    assert "make serve" in page, "does not say how to actually use it"


def test_demo_mode_disables_the_page_and_says_why() -> None:
    page = build_html(_run(), page="settings", ingest={"mode": "demo"})

    assert "<form" not in page, "demo mode still renders a box inviting a key"
    assert "Disabled in this deployment" in page
    assert "DEMO_MODE=1" in page


def test_without_keys_the_page_explains_what_it_would_do() -> None:
    """Functional without credentials, per the brief: it explains rather than erroring."""
    for ingest in ({"mode": "static"}, {"mode": "demo"}, {"mode": "ready"}):
        page = build_html(_run(), page="settings", ingest=ingest)
        assert "one</strong> settlement" in page or "one settlement" in page
        assert "GST" in page, "does not explain the question it exists to answer"
        assert "<h1>" in page and "<nav" in page, "the page stopped being a page"


def test_the_form_appears_only_where_it_can_act() -> None:
    ready = build_html(_run(), page="settings", ingest={"mode": "ready"})

    assert "<form" in ready
    assert 'method="post"' in ready, "a credential must not travel in a query string"
    assert 'type="password"' in ready, "the secret is shoulder-readable"
    assert 'autocomplete="off"' in ready


def test_the_limits_are_stated_on_the_page_itself() -> None:
    """Item 4e: test mode, read-only, one at a time, and the figures are synthetic."""
    page = build_html(_run(), page="settings", ingest={"mode": "ready"})
    text = re.sub(r"<[^>]+>", " ", page).lower()

    assert "test mode only" in text
    assert "read-only" in text
    assert "one settlement at a time" in text
    assert "synthetic" in text, "does not say the accuracy figures are not measured on this"


def test_the_result_shows_the_canonical_records_and_what_was_skipped() -> None:
    ingest = {
        "mode": "result",
        "key_id": "rzp_test_abc",
        "settlement_id": "setl_A",
        "rows": [
            {
                "row_id": "pay_1",
                "type": "payment",
                "credit_paise": 140254_00,
                "debit_paise": 0,
                "fee_base_paise": 2805_00,
                "gst_paise": 505_00,
                "net_paise": 136944_00,
            }
        ],
        "skipped": [("tr_1", "type 'transfer' is out of scope")],
        "settlement_ids": ["setl_A", "setl_B"],
        "net_total_paise": 136944_00,
    }
    page = build_html(_run(), page="settings", ingest=ingest)

    assert "pay_1" in page
    assert "Rs 1,40,254.00" in page, "money is not formatted at the render boundary"
    assert "Rs 2,805.00" in page and "Rs 505.00" in page, "the fee split is not shown"
    assert "Not adapted" in page and "tr_1" in page, "skipped rows are hidden"
    assert "setl_B" in page, "other settlements in the report are not named"


# --- the endpoint -------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DEMO_MODE", "0")
    from api.main import app

    return TestClient(app)


def test_demo_mode_refuses_the_post_before_reading_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed instance must have no path that accepts a credential at all."""
    monkeypatch.setenv("DEMO_MODE", "1")
    from api.main import app

    def explode(**_kwargs: object) -> None:
        raise AssertionError("demo mode attempted a fetch")

    monkeypatch.setattr("core.ingest.razorpay.fetch_recon", explode)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            content="key_id=rzp_test_a&key_secret=s&settlement_id=setl_A&year=2026&month=3",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    assert "Disabled in this deployment" in response.text
    assert "<form" not in response.text


def test_a_missing_field_is_reported_without_a_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(**_kwargs: object) -> None:
        raise AssertionError("a request was made with incomplete input")

    monkeypatch.setattr("core.ingest.razorpay.fetch_recon", explode)

    response = client.post(
        "/settings",
        content="key_id=rzp_test_a&key_secret=&settlement_id=setl_A&year=2026&month=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert "all required" in response.text


def test_a_successful_read_renders_the_adapted_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the endpoint, with the network replaced."""
    from tests.test_ingest_razorpay import _row

    monkeypatch.setattr(
        "core.ingest.razorpay.fetch_recon",
        lambda **_kwargs: [_row(settlement_id="setl_A", entity_id="pay_9")],
    )

    response = client.post(
        "/settings",
        content="key_id=rzp_test_a&key_secret=s3cret&settlement_id=setl_A&year=2026&month=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert "pay_9" in response.text
    assert "Canonical records" in response.text


def test_a_non_test_key_is_warned_about_but_still_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prefix convention is unverified (Q-016), so it warns rather than refuses."""
    from tests.test_ingest_razorpay import _row

    monkeypatch.setattr(
        "core.ingest.razorpay.fetch_recon",
        lambda **_kwargs: [_row(settlement_id="setl_A")],
    )

    response = client.post(
        "/settings",
        content="key_id=rzp_live_a&key_secret=s&settlement_id=setl_A&year=2026&month=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert "may not be a test-mode key" in response.text


def test_a_fetch_failure_is_reported_without_the_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.ingest.razorpay import ReconFetchError

    def refuse(**_kwargs: object) -> None:
        raise ReconFetchError("the gateway refused the request with HTTP 401.")

    monkeypatch.setattr("core.ingest.razorpay.fetch_recon", refuse)

    response = client.post(
        "/settings",
        content="key_id=rzp_test_a&key_secret=hunter2xyz&settlement_id=setl_A&year=2026&month=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert "That did not work" in response.text
    assert "HTTP 401" in response.text
    assert "hunter2xyz" not in response.text, "the secret reached the rendered page"


def test_the_run_payload_never_carries_ingest_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` is inlined verbatim on every page, so nothing credential-shaped may enter it."""
    from tests.test_ingest_razorpay import _row

    monkeypatch.setattr(
        "core.ingest.razorpay.fetch_recon",
        lambda **_kwargs: [_row(settlement_id="setl_A")],
    )

    response = client.post(
        "/settings",
        content="key_id=rzp_test_abc&key_secret=s3cret&settlement_id=setl_A&year=2026&month=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    block = re.search(
        r'<script id="run-data" type="application/json">(.*?)</script>', response.text, re.S
    ).group(1)
    assert "s3cret" not in block
    assert "rzp_test_abc" not in block
    assert "ingest" not in block


def test_nothing_is_written_to_disk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The credential is process-lifetime only: not `.env`, not a file, not anywhere."""
    from tests.test_ingest_razorpay import _row

    monkeypatch.setattr(
        "core.ingest.razorpay.fetch_recon",
        lambda **_kwargs: [_row(settlement_id="setl_A")],
    )

    from core.config import DOTENV_PATH

    before = DOTENV_PATH.read_text(encoding="utf-8") if DOTENV_PATH.exists() else None

    client.post(
        "/settings",
        content="key_id=rzp_test_abc&key_secret=s3cret&settlement_id=setl_A&year=2026&month=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    after = DOTENV_PATH.read_text(encoding="utf-8") if DOTENV_PATH.exists() else None
    assert before == after, ".env was modified by a settings submission"

    # It is in the environment, which is what "stored in the environment" means, and nowhere else.
    assert os.environ.get("RAZORPAY_KEY_SECRET") == "s3cret"
    del os.environ["RAZORPAY_KEY_SECRET"]
    del os.environ["RAZORPAY_KEY_ID"]
