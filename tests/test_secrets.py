"""Credentials must not reach disk, the audit ledger, a rendered page, or an error message.

The interesting assertion here is the **canary**: a key-shaped string is put into the process
environment and pushed through a real settings submission, and then every artefact the system
produces is searched for it. That is stronger than reading the code and concluding it looks fine,
because it fails if a future change starts serialising the environment into any of them.

The canary literals are assembled by concatenation rather than written whole. A key-shaped
literal in a committed file would be caught by this repository's own pre-commit scanner, and the
alternative — adding `pragma: allow-secret` to a test fixture — would blunt the hook for the sake
of a test. Splitting the string keeps the allowlist empty, which is the stronger position.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.check_secrets import scan

REPO_ROOT = Path(__file__).resolve().parents[1]

# Split so the committed file contains no key-shaped literal. See the module docstring.
CANARY_KEY_ID = "rzp_" + "test_" + "CANARY9876543210"
CANARY_SECRET = "CANARYSECRET" + "9876543210abcdef"


# --- the scanner can fire -------------------------------------------------------------------


def test_the_scanner_catches_every_shape_it_claims_to() -> None:
    """A guard never seen to fail has not been shown to work.

    Mutation-style: each line below *should* be blocked, and the test fails if the scanner lets
    one through. Written this way because the scanner's value is entirely in what it rejects.
    """
    should_block = [
        ("a live key id", f"key = '{'rzp_' + 'live_'}ABCDEFGH1234'"),
        ("a test key id", f"key = '{CANARY_KEY_ID}'"),
        ("a key secret", "RAZORPAY_KEY_SECRET = \"abcdefghijklmnop1234\""),
        ("an anthropic key", f"k = '{'sk-' + 'ant-'}abcdefghijklmnopqrst'"),
        # Split for the same reason as the canaries above: written whole, these two lines are
        # themselves key-shaped and this repository's pre-commit hook blocks the commit. It
        # caught them, which is the scanner passing its own test on the way in.
        ("an aws key id", "AKIA" + "IOSFODNN7EXAMPLE"),
        ("a private key", "-----BEGIN RSA " + "PRIVATE KEY-----"),
    ]
    for label, line in should_block:
        findings = scan([("f.py", 1, line)])
        assert findings, f"the scanner does not catch {label}"


def test_the_scanner_does_not_cry_wolf() -> None:
    """A scanner that fires on ordinary code gets disabled, and a disabled scanner protects nothing."""
    benign = [
        "RAZORPAY_KEY_ID is entered in the UI and held in the process",
        'TEST_KEY_PREFIX = "rzp_test_"',
        'placeholder="rzp_test_…"',
        "key_id: str, key_secret: str",
    ]
    for line in benign:
        assert not scan([("f.py", 1, line)]), f"false positive on: {line}"


def test_the_allowlist_is_not_being_used_to_smuggle_anything() -> None:
    """The escape hatch exists; this asserts nothing is actually being suppressed by it.

    The property is **per line**, not per file: the marker only suppresses anything when it sits
    on a line that would otherwise match a pattern. Prose may name it freely — `docs/DECISIONS.md`
    D-0009 explains why the hatch exists, and this module's own docstring explains why it is not
    used — and neither suppresses a thing.

    The first version of this test grepped whole files for the marker and failed on those very
    mentions. That is the proxy-metric failure this repository keeps logging: it measured "the
    string appears somewhere" as a stand-in for "a secret is being allowed through". So it now
    checks the real condition — a line carrying the marker that would match a pattern without it.
    """
    import subprocess

    from scripts.check_secrets import ALLOWLIST_MARKER, PATTERNS

    hits = subprocess.run(
        ["git", "grep", "-n", ALLOWLIST_MARKER, "--", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    suppressing = []
    for hit in hits:
        path, _, rest = hit.partition(":")
        _lineno, _, text = rest.partition(":")
        stripped = text.replace(ALLOWLIST_MARKER, "")
        if any(pattern.search(stripped) for _label, pattern in PATTERNS):
            suppressing.append(path)

    assert not suppressing, f"the allowlist is suppressing a real match in: {suppressing}"


# --- the adapter cannot write ---------------------------------------------------------------


def test_the_adapter_has_no_write_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only is structural (D-0027): there is no verb other than GET in the module."""
    source = (REPO_ROOT / "core" / "ingest" / "razorpay.py").read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert not re.search(rf'"{verb}"|\'{verb}\'', source), f"{verb} appears in the adapter"
    assert source.count('method="GET"') == 1


def test_a_transport_failure_message_never_carries_the_credential() -> None:
    """`urllib` puts the request into the exception it raises; the message is rebuilt regardless."""
    import urllib.error

    from core.ingest import razorpay

    def raise_http(request: Any, timeout: int = 0) -> None:
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", hdrs=None, fp=None
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(razorpay.urllib.request, "urlopen", raise_http)
        with pytest.raises(razorpay.ReconFetchError) as caught:
            razorpay.fetch_recon(
                key_id=CANARY_KEY_ID, key_secret=CANARY_SECRET, year=2026, month=3
            )

    message = str(caught.value)
    assert CANARY_SECRET not in message
    assert CANARY_KEY_ID not in message
    assert "401" in message, "the message stopped being useful"


# --- the canary -----------------------------------------------------------------------------


def test_a_key_shaped_string_reaches_neither_the_ledger_nor_any_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The item-5 assertion, end to end.

    A credential is placed in the environment and submitted through `/settings`, and then every
    artefact this system produces is searched for it: the audit ledger's canonical text, the
    SQLite the ledger writes, and all five rendered pages including their inlined JSON.
    """
    from eval.provenance import GENERATED_DIR, dataset_files

    if not dataset_files("dev_seed_11", generated_dir=GENERATED_DIR):
        pytest.skip("datasets not generated; run `make seed`")

    monkeypatch.setenv("RAZORPAY_KEY_ID", CANARY_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", CANARY_SECRET)
    monkeypatch.setenv("DEMO_MODE", "0")

    from tests.test_ingest_razorpay import _row

    monkeypatch.setattr(
        "core.ingest.razorpay.fetch_recon",
        lambda **_kwargs: [_row(settlement_id="setl_A")],
    )

    from api.main import app

    with TestClient(app) as client:
        submitted = client.post(
            "/settings",
            content=(
                f"key_id={CANARY_KEY_ID}&key_secret={CANARY_SECRET}"
                "&settlement_id=setl_A&year=2026&month=3"
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert submitted.status_code == 200

        # The secret must not come back on the page that accepted it, in any form.
        assert CANARY_SECRET not in submitted.text

        # The key id is echoed into the form field so it need not be retyped; nothing else.
        echoed = re.findall(re.escape(CANARY_KEY_ID), submitted.text)
        assert len(echoed) == 1, f"the key id appears {len(echoed)} times, expected once"
        block = re.search(
            r'<script id="run-data" type="application/json">(.*?)</script>', submitted.text, re.S
        ).group(1)
        assert CANARY_KEY_ID not in block and CANARY_SECRET not in block

    # --- the audit ledger, in both the forms it is written in ---
    from eval import harness

    db = tmp_path / "canary.db"
    metrics = harness.evaluate("dev_seed_11", db_path=db)

    ledger_text = "".join(
        json.dumps({**entry.payload(), "entry_hash": entry.entry_hash}, sort_keys=True)
        for entry in metrics.ledger
    )
    assert CANARY_KEY_ID not in ledger_text
    assert CANARY_SECRET not in ledger_text

    rows = sqlite3.connect(db).execute("SELECT * FROM audit_log").fetchall()
    dumped = "".join(str(cell) for row in rows for cell in row)
    assert CANARY_SECRET not in dumped
    assert CANARY_KEY_ID not in dumped

    # --- every rendered page, including the inlined JSON ---
    from scripts import render_report

    assert render_report.main(["--out-dir", str(tmp_path)]) == 0
    for filename, *_rest in render_report.PAGES.values():
        page = (tmp_path / filename).read_text(encoding="utf-8")
        assert CANARY_SECRET not in page, f"the secret reached {filename}"
        assert CANARY_KEY_ID not in page, f"the key id reached {filename}"


def test_the_ledger_has_no_field_that_could_carry_a_credential() -> None:
    """Structural, not incidental: the entry schema has nowhere to put one.

    The canary test above proves nothing leaks *today*. This one says the shape of a ledger entry
    gives a future change no obvious place to start.
    """
    from audit.ledger import LedgerEntry

    fields = set(LedgerEntry.__dataclass_fields__)
    # `input_tokens` and `output_tokens` are LLM usage counts and are meant to be there, so the
    # patterns name credential-shaped words specifically rather than the substring "token",
    # which would flag them. A test that has to be argued with on every run gets deleted.
    suspicious = (
        "key",
        "secret",
        "credential",
        "password",
        "api_token",
        "access_token",
        "auth",
        "environ",
    )
    for word in suspicious:
        matching = [f for f in fields if word in f.lower()]
        assert not matching, f"ledger entry has a {word}-shaped field: {matching}"


def test_dotenv_is_never_written_by_any_module() -> None:
    """`core/config.py` reads `.env`. Nothing in the tree may write it."""
    import subprocess

    hits = subprocess.run(
        ["git", "grep", "-n", "-E", r"DOTENV_PATH\.(write|open\(.w)", "--", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not hits, f"something writes .env: {hits}"
