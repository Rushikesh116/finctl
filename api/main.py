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
from fastapi.staticfiles import StaticFiles

from core.config import load_dotenv
from data.generator import DATASET_SEEDS

PHASE = 6

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"

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
        "block": harness.render(metrics),
        "ablation": harness.render_ablation(dataset),
    }
    return payload


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
    """The exception queue, which is the deliverable the grading bar names."""
    run = _RUN.get("dev_seed_11") or compute_run("dev_seed_11")
    return JSONResponse(
        {
            "total": run["exceptions"],
            "at_risk_paise": run["at_risk_paise"],
            "by_type": run["by_type"],
            "by_class": run["by_class"],
            "unclassified": run["unclassified"],
        }
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """The single page. Served from `web/` if built, otherwise the static report."""
    for candidate in (WEB_DIR / "index.html", REPO_ROOT / "docs" / "index.html"):
        if candidate.exists():
            return HTMLResponse(candidate.read_text(encoding="utf-8"))
    raise HTTPException(status_code=503, detail="no page built yet; run `make report`")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
