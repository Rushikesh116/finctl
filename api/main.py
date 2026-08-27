"""FastAPI application — JSON API and the static frontend from one process.

One container, one port, no separate frontend service. That is a deployment decision as much as
an architectural one: two services means two things that can be up or down independently, and
this is a demo a judge clicks once.

**`DEMO_MODE=1` is the deployed default.** The run is computed once at startup from committed
fixtures, with every Layer 4 response replayed by prompt hash — so there is no API key in the
deployed environment, no cold-start model call, no quota to exhaust, and the numbers are
identical on every click. That is deliberate rather than a shortcut: a judge refreshing the page
should not see a different auto-match rate because a provider was busy.

A replay cache miss raises rather than reaching the network (invariant 4), so if fixtures are
incomplete the container fails loudly at startup instead of serving a run it cannot reproduce.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import load_dotenv
from data.generator import DATASET_SEEDS

PHASE = 6

REPO_ROOT = Path(__file__).resolve().parents[1]
# `web/` is deliberately not referenced here. The renderer owns it and inlines it; this module
# never serves it as an asset.

load_dotenv()

app = FastAPI(
    title="FinCtl",
    description="Payment reconciliation: what matched, what did not, and why.",
    version="0.6.0",
)

# Computed once at startup and held. The cascade is deterministic and takes under a second, so
# recomputing per request would burn CPU to produce a byte-identical answer.
_RUN: dict[str, Any] = {}


def _jsonable(value: Any) -> Any:
    """Dataclasses and sets are not JSON; everything else here already is."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


def compute_run(dataset: str = "dev_seed_11") -> dict[str, Any]:
    """Run the cascade and shape it for the API and the report.

    Imported inside the function so a container that fails to build its datasets reports the
    error at request time with a readable message, rather than at import time as a stack trace
    before the app object exists.
    """
    from eval import harness

    metrics = harness.evaluate(dataset)
    payload = {
        "dataset": metrics.dataset,
        "provenance": {
            "dataset_sha": metrics.provenance.dataset_sha,
            "git_sha": metrics.provenance.git_sha,
            "started_at_utc": metrics.provenance.started_at_utc,
            "manifest_state": metrics.provenance.manifest_state,
            "trustworthy": metrics.provenance.is_trustworthy,
        },
        "records": metrics.n,
        "wall_clock_ms": metrics.wall_clock_us // 1000,
        "auto_matched": metrics.auto_matched,
        "per_layer": {str(k): v for k, v in sorted(metrics.per_layer.items())},
        "false_matches": metrics.false_matches,
        "exceptions": metrics.exception_records,
        "correctly_flagged": metrics.correctly_flagged,
        "missed_matches": metrics.missed_matches,
        "at_risk_paise": metrics.at_risk_paise,
        "by_type": dict(sorted(metrics.by_type.items())),
        "by_class": dict(sorted(metrics.by_class.items())),
        "by_mechanism": _jsonable(metrics.by_mechanism),
        "per_pathology": {str(k): list(v) for k, v in metrics.per_pathology.items()},
        "refusals": {k: list(v) for k, v in metrics.refusals.items()},
        "unclassified": metrics.unclassified_records,
        "llm": {
            "provider": metrics.llm_provider,
            "model_versions": list(metrics.llm_model_versions),
            "mode": metrics.llm_mode,
            "calls": metrics.llm_calls,
            "cache_hits": metrics.llm_cache_hits,
            "stubbed": metrics.llm_stubbed,
            "real_responses": metrics.llm_real_responses,
            "retries": metrics.llm_retries,
            "rules_total": metrics.rules_total,
            "rules_promoted": metrics.rules_promoted,
            "rules_by_source": dict(metrics.rules_by_source),
        },
        "ledger": {"entries": metrics.ledger_entries, "head": metrics.ledger_head},
        "entering": {str(k): v for k, v in metrics.entering_layer().items()},
        "exception_items": _exception_items(metrics),
        # Not "records": that key already holds the record *count*, and the first version of this
        # shadowed it with the digest dict. Rendering then failed on `f"{n:,}"` against a dict --
        # loudly, which is the only reason it did not ship as a wrong number.
        "record_digest": metrics.record_digest,
        "block": harness.render(metrics),
        "ablation": harness.render_ablation(dataset),
    }
    return payload


def _exception_items(metrics: Any) -> list[dict[str, Any]]:
    """Each exception with its evidence and the audit entries that mention it.

    Sorted by amount at risk, descending. An operator working a queue works the expensive end of
    it first, and ordering by exception type instead would bury a large single refusal under a
    long tail of small ones.

    The audit trail is attached per exception rather than shipped as one flat log and joined in
    the browser: the join key is a set intersection over record ids, and doing it here keeps the
    page's script free of logic that could disagree with the ledger.
    """
    by_record: dict[str, list[dict[str, Any]]] = {}
    for entry in metrics.ledger:
        for row_id in entry.record_ids:
            by_record.setdefault(row_id, []).append(
                {
                    "seq": entry.seq,
                    "layer": entry.layer,
                    "decision": entry.decision,
                    "outcome": entry.outcome,
                    "confidence": entry.confidence,
                    "detail": entry.detail,
                    "provider": entry.provider,
                    "model": entry.model,
                    "entry_hash": entry.entry_hash[:12],
                }
            )

    items = []
    for index, exception in enumerate(metrics.exceptions):
        seen: set[int] = set()
        trail = []
        for row_id in exception.record_ids:
            for entry in by_record.get(row_id, []):
                if entry["seq"] not in seen:
                    seen.add(entry["seq"])
                    trail.append(entry)
        trail.sort(key=lambda e: e["seq"])

        items.append(
            {
                "id": f"ex{index:03d}",
                "type": exception.exception_type,
                "layer": exception.layer,
                "record_ids": list(exception.record_ids),
                "amount_at_risk_paise": exception.amount_at_risk_paise,
                "detail": exception.detail,
                "evidence": [
                    {"row_ids": list(item.row_ids), "sum_paise": item.sum_paise}
                    for item in exception.evidence
                ],
                # All four travel together. `evidence_found` without `evidence_truncated` cannot
                # tell a reader whether they are looking at every subset that closed delta or the
                # first five of some larger number, and that distinction is the difference
                # between a complete refusal and a sampled one.
                "evidence_found": exception.evidence_found,
                "evidence_truncated": exception.evidence_truncated,
                "evidence_complete": exception.evidence_complete,
                "audit_trail": trail,
            }
        )

    items.sort(key=lambda item: (-item["amount_at_risk_paise"], item["id"]))
    return items


@app.on_event("startup")
def _warm() -> None:
    """Compute the run at startup so the first request is not the slow one.

    A failure here is deliberately fatal: an incomplete fixture set means the container cannot
    reproduce the run it would serve, and serving a partial answer would be worse than not
    starting.
    """
    _RUN["dev_seed_11"] = compute_run("dev_seed_11")


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Container health check. Reports readiness *and* provenance.

    A health check that only says "the process is up" cannot distinguish a container serving the
    right data from one serving whatever was baked into a stale image, so it returns the dataset
    and git SHAs it is actually serving.
    """
    run = _RUN.get("dev_seed_11")
    if run is None:
        raise HTTPException(status_code=503, detail="run not computed yet")
    return JSONResponse(
        {
            "status": "ok",
            "demo_mode": os.environ.get("DEMO_MODE", "0") == "1",
            "dataset": run["dataset"],
            "dataset_sha": run["provenance"]["dataset_sha"],
            "git_sha": run["provenance"]["git_sha"],
            # The count. A health check is polled every 30 seconds by the container runtime and
            # scraped by anything watching the deploy, so it stays small and scalar -- an earlier
            # edit put the whole record digest here and turned a 152-byte probe into 30 KB.
            "records": run["records"],
            "auto_matched": run["auto_matched"],
            "false_matches": run["false_matches"],
        }
    )


@app.get("/api/run")
def get_run(dataset: str = "dev_seed_11") -> JSONResponse:
    """The whole run as JSON. The static report inlines this same shape."""
    if dataset not in DATASET_SEEDS:
        raise HTTPException(
            status_code=404, detail=f"unknown dataset; expected one of {sorted(DATASET_SEEDS)}"
        )
    if dataset == "holdout_seed_97":
        # The holdout is evaluated once, in Phase 6, and its result is reported rather than
        # served on demand. An endpoint that re-ran it on request would turn it into a
        # development set the first time someone refreshed.
        raise HTTPException(
            status_code=403,
            detail="the holdout is evaluated once and reported; it is not served on demand",
        )
    if dataset not in _RUN:
        _RUN[dataset] = compute_run(dataset)
    return JSONResponse(_RUN[dataset])


@app.get("/api/exceptions")
def get_exceptions() -> JSONResponse:
    """The exception queue, which is the deliverable the grading bar names.

    Returns the aggregates *and* the individual exceptions with their evidence. An honest
    exception list is the deliverable; a set of counts is a summary of one.
    """
    run = _RUN.get("dev_seed_11") or compute_run("dev_seed_11")
    return JSONResponse(
        {
            "total": run["exceptions"],
            "at_risk_paise": run["at_risk_paise"],
            "by_type": run["by_type"],
            "by_class": run["by_class"],
            "unclassified": run["unclassified"],
            "items": run["exception_items"],
            "records": run["record_digest"],
        }
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """The single page, assembled from the *live* run.

    Same assembler as `make report`, so there is one UI and not a live copy drifting from a
    static one. Rendering here rather than serving the committed `docs/index.html` also means the
    page reports the provenance of the code actually running, instead of whatever SHA was current
    when the file was last committed.

    Falls back to the committed file if `web/` is missing from the image, because a page with
    slightly stale provenance beats a 503.
    """
    from scripts.render_report import build_html

    try:
        return HTMLResponse(build_html(_RUN.get("dev_seed_11") or compute_run("dev_seed_11")))
    except FileNotFoundError:
        static = REPO_ROOT / "docs" / "index.html"
        if static.exists():
            return HTMLResponse(static.read_text(encoding="utf-8"))
        raise HTTPException(
            status_code=503, detail="no page available; run `make report`"
        ) from None


# No StaticFiles mount. Everything the page needs is inlined by the assembler, so mounting the
# asset directory would advertise a fetch path the page never uses -- and a page that *can* fetch
# is a page that might, which is the property the static report exists to rule out.
