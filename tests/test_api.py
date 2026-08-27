"""The HTTP surface.

There were no tests here until a `/healthz` regression shipped to the live service: an unscoped
edit replaced the record *count* with the whole record digest, turning a 152-byte health probe into
a 30 KB payload. Nothing in the suite looked at the endpoint, so nothing failed. The container
caught it.

Routes are called as plain functions rather than through `fastapi.testclient.TestClient`.
`TestClient` needs `httpx`, which is present only as a transitive dependency of `google-genai` and
is not in `requirements.txt` — building the test suite on a package the project does not declare
would make these tests break on an unrelated upgrade.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from api import main as api


def _body(response: JSONResponse) -> Any:
    return json.loads(bytes(response.body).decode("utf-8"))


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    """The real run, computed once. Skips if the datasets have not been generated."""
    from eval.provenance import GENERATED_DIR, dataset_files

    if not dataset_files("dev_seed_11", generated_dir=GENERATED_DIR):
        pytest.skip("datasets not generated; run `make seed`")

    payload = api.compute_run("dev_seed_11")
    api._RUN["dev_seed_11"] = payload
    return payload


# --- /healthz -----------------------------------------------------------------------------


def test_healthz_reports_readiness_and_provenance(run: dict[str, Any]) -> None:
    body = _body(api.healthz())

    assert body["status"] == "ok"
    assert body["dataset"] == "dev_seed_11"
    assert body["dataset_sha"] and body["dataset_sha"] != "unknown"
    assert body["git_sha"]


def test_healthz_stays_scalar(run: dict[str, Any]) -> None:
    """The regression this file was written for.

    Every field must be a scalar. A health check is polled every 30 seconds by the container
    runtime, so a nested structure here is both a performance problem and a sign that a field is
    reporting something other than what its name says.
    """
    body = _body(api.healthz())

    for key, value in body.items():
        assert isinstance(
            value, (str, int, float, bool)
        ), f"/healthz field {key!r} is {type(value).__name__}, not a scalar"

    assert isinstance(body["records"], int), "records must be the count, not the digest"
    assert body["records"] == run["records"]


def test_healthz_probe_stays_small(run: dict[str, Any]) -> None:
    """A budget, not an exact size. The digest version of this was ~30 KB."""
    assert len(bytes(api.healthz().body)) < 1024


def test_healthz_refuses_before_the_run_is_computed() -> None:
    """503 rather than a partial answer: an uncomputed run has no numbers to report."""
    saved = api._RUN.pop("dev_seed_11", None)
    try:
        with pytest.raises(HTTPException) as raised:
            api.healthz()
        assert raised.value.status_code == 503
    finally:
        if saved is not None:
            api._RUN["dev_seed_11"] = saved


# --- /api/run -----------------------------------------------------------------------------


def test_run_endpoint_returns_the_whole_run(run: dict[str, Any]) -> None:
    body = _body(api.get_run("dev_seed_11"))

    assert body["records"] == run["records"]
    assert body["auto_matched"] == run["auto_matched"]
    assert len(body["exception_items"]) > 0
    assert body["entering"]["1"] == body["records"]


def test_holdout_is_never_served_on_demand() -> None:
    """403, by design.

    An endpoint that re-ran the holdout on request would convert it into a development set the
    first time anyone refreshed the page. It is evaluated once and the result is reported.
    """
    with pytest.raises(HTTPException) as raised:
        api.get_run("holdout_seed_97")

    assert raised.value.status_code == 403
    assert "once" in raised.value.detail


def test_unknown_dataset_is_404_and_names_the_valid_ones() -> None:
    with pytest.raises(HTTPException) as raised:
        api.get_run("no_such_dataset")

    assert raised.value.status_code == 404
    assert "dev_seed_11" in raised.value.detail


# --- /api/exceptions ----------------------------------------------------------------------


def test_exceptions_endpoint_carries_items_not_only_counts(run: dict[str, Any]) -> None:
    """An honest exception list is the deliverable; a set of counts is a summary of one."""
    body = _body(api.get_exceptions())

    assert body["total"] == run["exceptions"]
    assert len(body["items"]) == len(run["exception_items"])
    assert body["records"], "evidence records are missing, so the items cannot be read"


def test_every_exception_item_carries_all_four_evidence_fields(run: dict[str, Any]) -> None:
    """`evidence_found` alone cannot distinguish a complete refusal from a sampled one."""
    for item in _body(api.get_exceptions())["items"]:
        for field in ("evidence", "evidence_found", "evidence_truncated", "evidence_complete"):
            assert field in item, f"{item['id']} is missing {field}"
        assert len(item["evidence"]) <= item["evidence_found"] or item["evidence_found"] == 0


def test_items_are_ordered_by_amount_at_risk(run: dict[str, Any]) -> None:
    """An operator works the expensive end of the queue first."""
    amounts = [item["amount_at_risk_paise"] for item in _body(api.get_exceptions())["items"]]
    assert amounts == sorted(amounts, reverse=True)


def test_evidence_row_ids_all_resolve_in_the_digest(run: dict[str, Any]) -> None:
    """Evidence must be readable without joining back to the source files.

    A row id in the evidence with no matching digest entry means the page renders an
    unresolvable reference, which defeats the purpose of recording evidence at all.
    """
    body = _body(api.get_exceptions())
    digest = body["records"]

    for item in body["items"]:
        for row_id in item["record_ids"]:
            assert row_id in digest, f"{item['id']} names {row_id}, absent from the digest"
        for evidence in item["evidence"]:
            for row_id in evidence["row_ids"]:
                assert row_id in digest, f"{item['id']} evidence names {row_id}, absent"


def test_audit_trail_entries_reference_the_exception_they_are_attached_to(
    run: dict[str, Any],
) -> None:
    for item in _body(api.get_exceptions())["items"]:
        for entry in item["audit_trail"]:
            assert entry["entry_hash"], "a ledger entry without its hash is not an audit record"
            assert entry["layer"] >= 1


# --- the page -----------------------------------------------------------------------------


def test_index_renders_from_the_live_run(run: dict[str, Any]) -> None:
    """Same assembler as `make report`, so the live page cannot drift from the static one."""
    page = api.index().body.decode("utf-8")

    assert "<!DOCTYPE html>" in page
    assert page.count("<script") == 1, "the live page must not gain a script"
    assert run["provenance"]["git_sha"] in page
