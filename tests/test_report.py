"""The rendered page.

Asserted as properties rather than as strings wherever possible. "The page contains no `src=`"
is checkable; "the page looks right" is not, and a test that pins exact markup would fail on every
wording change while still passing if the numbers were wrong.

The self-containment tests are the load-bearing ones: the static report's whole claim is that it
works with no server, no network, and no script, and that claim is one stray `<script src>` away
from being false.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import render_report
from scripts.render_report import build_html, label_for

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(**overrides: Any) -> dict[str, Any]:
    """A minimal but complete run payload. Overridden per test rather than mutated in place."""
    run: dict[str, Any] = {
        "dataset": "dev_seed_11",
        "provenance": {
            "dataset_sha": "1115450f",
            "git_sha": "abc1234",
            "started_at_utc": "2026-08-27 12:00",
            "manifest_state": "match",
            "trustworthy": True,
        },
        "records": 100,
        "wall_clock_ms": 42,
        "auto_matched": 80,
        "per_layer": {"1": 50, "2": 20, "3": 0, "4": 10},
        "entering": {"1": 100, "2": 50, "3": 30, "4": 30},
        "false_matches": 0,
        "exceptions": 20,
        "correctly_flagged": 18,
        "missed_matches": 2,
        "at_risk_paise": 123456789,
        "by_type": {"AMBIGUOUS": 12, "MISSING_BANK_ROW": 8},
        "by_class": {"absent": 10, "undetermined": 8},
        "unclassified": 0,
        "llm": {
            "provider": "google-gemini",
            "model_versions": ["gemini-3.7-flash"],
            "mode": "replay",
            "calls": 0,
            "cache_hits": 6,
            "stubbed": False,
            "real_responses": 2,
            "retries": 0,
            "rules_total": 3,
            "rules_promoted": 2,
            "rules_by_source": {},
        },
        "ledger": {"entries": 12, "head": "deadbeefcafe0000"},
        "exception_items": [],
        "record_digest": {},
        "block": "metrics block text",
        "ablation": "ablation text",
    }
    run.update(overrides)
    return run


def _item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "ex000",
        "type": "AMBIGUOUS",
        "layer": 3,
        "record_ids": ["ml_1", "gw_1"],
        "amount_at_risk_paise": 5000,
        "detail": "two candidates fit equally",
        "evidence": [{"row_ids": ["ml_1", "gw_1"], "sum_paise": 2500}],
        "evidence_found": 1,
        "evidence_truncated": False,
        "evidence_complete": True,
        "audit_trail": [],
    }
    item.update(overrides)
    return item


def _digest(**overrides: Any) -> dict[str, Any]:
    base = {
        "ml_1": {
            "source": "ledger",
            "kind": "order",
            "amount_paise": 2500,
            "currency": "INR",
            "at_utc": 1773133200,
            "keys": {"order_ref": "receipt#1"},
        },
        "gw_1": {
            "source": "gateway",
            "kind": "payment",
            "amount_paise": 2500,
            "net_paise": 2400,
            "currency": "INR",
            "at_utc": 1773135000,
            "keys": {"payment_id": None, "order_id": None},
        },
    }
    base.update(overrides)
    return base


# --- self-containment ---------------------------------------------------------------------


def test_page_has_no_external_reference_of_any_kind() -> None:
    """The static report's entire premise. One `src=` and it is false."""
    page = build_html(_run())

    assert not re.search(r"\bsrc\s*=", page), "page fetches an external asset"
    assert not re.search(r'href\s*=\s*"(?:https?:)?//', page), "page links out for an asset"
    assert "@import" not in page
    assert not re.search(r'url\(\s*["\']?(?:https?:)?//', page), "stylesheet fetches remotely"


def test_page_runs_no_script() -> None:
    """Expansion is native `<details>`. Nothing on the page executes.

    The only `<script>` permitted is the inert JSON data block, which browsers do not run.
    """
    page = build_html(_run())

    scripts = re.findall(r"<script([^>]*)>", page)
    assert len(scripts) == 1, f"expected exactly one script element, found {len(scripts)}"
    assert 'type="application/json"' in scripts[0]

    assert not re.search(r"\bfetch\s*\(|XMLHttpRequest", page)
    assert not re.search(r'\son(?:click|load|change|submit|error)\s*=', page)


def test_inlined_data_cannot_break_out_of_its_block() -> None:
    """`</` is the one sequence that can terminate a script element early."""
    hostile = _item(detail="</script><script>alert(1)</script>")
    page = build_html(_run(exception_items=[hostile], record_digest=_digest()))

    assert "<script>alert(1)</script>" not in page
    block = re.search(
        r'<script id="run-data" type="application/json">(.*?)</script>', page, re.S
    ).group(1)
    assert "</script" not in block
    # And it must still be valid JSON after escaping.
    json.loads(block.replace("<\\/", "</"))


def test_every_template_token_is_substituted() -> None:
    page = build_html(_run())
    assert not re.findall(r"%%[A-Z_]+%%", page)


# --- the cascade --------------------------------------------------------------------------


def test_cascade_has_five_bars_and_widths_come_from_entering_counts() -> None:
    """Four layers plus the queue, sized by arrivals — not by what each layer resolved."""
    page = build_html(_run())
    rungs = re.findall(r'<div class="rung[^"]*">.*?width:([\d.]+)%', page, re.S)

    assert len(rungs) == 5, f"expected five bars, found {len(rungs)}"
    widths = [float(w) for w in rungs]
    # entering = 100, 50, 30, 30 of 100 records; queue = 20.
    assert widths == [100.0, 50.0, 30.0, 30.0, 20.0]


def test_cascade_widths_never_widen_down_the_funnel() -> None:
    """Each layer sees only the residue of the one above, so arrivals cannot increase.

    A widening bar would mean the cascade is being drawn from something other than the funnel.
    """
    page = build_html(_run())
    widths = [
        float(w) for w in re.findall(r'<div class="rung[^"]*">.*?width:([\d.]+)%', page, re.S)
    ]
    assert widths == sorted(widths, reverse=True)


def test_a_layer_that_resolved_nothing_is_marked_not_hidden() -> None:
    """Layer 3 resolves zero on the real dataset. That is a result, and it must be visible."""
    page = build_html(_run())
    assert 'class="rung dead"' in page
    assert "settled none of them" in page


# --- labels -------------------------------------------------------------------------------


def test_ambiguous_is_labelled_differently_at_layer_2_and_layer_3() -> None:
    """Same exception type, genuinely different claims — one label for both describes neither."""
    assert label_for("AMBIGUOUS", 2) != label_for("AMBIGUOUS", 3)
    assert "subset" in label_for("AMBIGUOUS", 2).lower()


def test_every_exception_type_has_a_reader_facing_label() -> None:
    """The closed enum and the label table must not drift apart."""
    from core.results import EXCEPTION_TYPES

    for exception_type in sorted(EXCEPTION_TYPES):
        label = label_for(exception_type)
        assert label != exception_type, f"{exception_type} has no reader-facing label"
        assert "_" not in label, f"{label!r} still reads like an internal constant"
        assert label[0].isupper() and not label.isupper(), f"{label!r} is not sentence case"


def test_the_internal_type_is_still_shown_but_not_as_the_label() -> None:
    """An operator needs the plain-language reason; a developer triaging needs the constant.

    Both appear — the label leads, the type is secondary — so this asserts the ordering rather
    than the absence of the constant.
    """
    page = build_html(_run(exception_items=[_item()], record_digest=_digest()))
    label = label_for("AMBIGUOUS", 3)
    assert page.index(label) < page.index("AMBIGUOUS<")


# --- evidence -----------------------------------------------------------------------------


def test_truncated_evidence_says_so_with_both_counts() -> None:
    """`showing 5 of 21` is the honest form. `5 subsets` would be a different claim."""
    item = _item(
        layer=2,
        evidence=[{"row_ids": ["gw_1", "gw_2"], "sum_paise": 100} for _ in range(5)],
        evidence_found=21,
        evidence_truncated=True,
    )
    page = build_html(_run(exception_items=[item], record_digest=_digest()))

    assert "showing 5 of 21" in page
    assert "truncated" in page


def test_incomplete_evidence_is_reported_as_a_lower_bound() -> None:
    """A search stopped at its bound has not shown that no subset exists."""
    item = _item(
        type="SUBSET_SEARCH_EXHAUSTED",
        layer=2,
        evidence=[],
        evidence_found=0,
        evidence_complete=False,
    )
    page = build_html(_run(exception_items=[item], record_digest=_digest()))

    assert "lower bound" in page
    assert "stopping rule" in page


def test_absent_evidence_and_exhausted_search_read_differently() -> None:
    """Conflating "nothing to weigh" with "gave up before finding anything" is the D-0014 error."""
    nothing_to_weigh = build_html(
        _run(
            exception_items=[_item(evidence=[], evidence_found=0, evidence_complete=True)],
            record_digest=_digest(),
        )
    )
    gave_up = build_html(
        _run(
            exception_items=[_item(evidence=[], evidence_found=0, evidence_complete=False)],
            record_digest=_digest(),
        )
    )

    assert "lower bound" in gave_up
    assert "lower bound" not in nothing_to_weigh


def test_evidence_records_carry_amount_and_keys_not_just_ids() -> None:
    """A row id alone does not let a reader check that two candidates are indistinguishable."""
    page = build_html(_run(exception_items=[_item()], record_digest=_digest()))

    assert "ml_1" in page and "gw_1" in page
    assert "Rs 25.00" in page, "evidence record is missing its amount"
    assert "receipt#1" in page, "evidence record is missing its identifying key"
    assert "no identifying key on this row" in page, "a keyless row must say so explicitly"


def test_timestamps_render_as_dates_not_epochs() -> None:
    """`1773133200` beside an amount tells an operator nothing."""
    page = build_html(_run(exception_items=[_item()], record_digest=_digest()))

    assert "1773133200" not in page.split('<script id="run-data"')[0]
    assert "IST" in page


def test_every_exception_shows_the_records_it_names() -> None:
    """Including the ones with no competing candidates, where those records *are* the basis."""
    item = _item(
        type="MISSING_BANK_ROW", layer=1, evidence=[], evidence_found=0, record_ids=["gw_1"]
    )
    page = build_html(_run(exception_items=[item], record_digest=_digest()))

    assert "Records named" in page
    assert "gw_1" in page


# --- money --------------------------------------------------------------------------------


def test_money_is_formatted_with_indian_grouping_at_the_render_boundary() -> None:
    page = build_html(_run(at_risk_paise=123456789))
    # 123456789 paise = Rs 12,34,567.89 — lakh grouping, not thousands.
    assert "Rs 12,34,567.89" in page
    assert "Rs 1,234,567.89" not in page


def test_renderer_never_converts_money_to_float() -> None:
    """Invariant 1 at the presentation layer: paise go in, a string comes out."""
    source = (REPO_ROOT / "scripts" / "render_report.py").read_text(encoding="utf-8")
    assert "float(" not in source.replace("float(w)", ""), "renderer coerces a value to float"
    assert "/ 100" not in source, "renderer divides paise into rupees itself"


# --- empty states -------------------------------------------------------------------------


def test_empty_queue_says_what_to_run_and_what_to_distrust() -> None:
    page = build_html(_run(exception_items=[], exceptions=0, by_type={}))
    assert "Nothing in the queue" in page
    assert "make eval" in page


def test_no_records_says_what_to_run() -> None:
    page = build_html(_run(records=0, auto_matched=0, exceptions=0, exception_items=[]))
    assert "make demo" in page


def test_missing_audit_trail_says_what_to_run() -> None:
    page = build_html(_run(exception_items=[_item(audit_trail=[])], record_digest=_digest()))
    assert "make run" in page


# --- warnings -----------------------------------------------------------------------------


def test_dataset_drift_is_stated_inline_not_footnoted() -> None:
    run = _run()
    run["provenance"]["manifest_state"] = "drift"
    page = build_html(run)
    assert "Dataset drift" in page
    assert "make seed" in page


def test_stubbed_adjudication_is_disclosed() -> None:
    run = _run()
    run["llm"]["stubbed"] = True
    page = build_html(run)
    assert "offline stub" in page
    assert "METRICS.md" in page


def test_no_banner_when_there_is_nothing_to_warn_about() -> None:
    assert 'class="warn"' not in build_html(_run())


# --- accessibility and responsiveness -----------------------------------------------------


def test_stylesheet_respects_reduced_motion_and_shows_focus() -> None:
    css = (REPO_ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert "prefers-reduced-motion" in css
    assert "focus-visible" in css
    assert "summary:focus-visible" in css, "summary focus is unstyled in several UA stylesheets"
    assert "@media (max-width" in css, "no small-screen handling"
    assert "prefers-color-scheme" in css


def test_palette_is_grayscale_plus_exactly_two_accents() -> None:
    """A third accent would imply a third outcome, and there are only two."""
    css = (REPO_ROOT / "web" / "app.css").read_text(encoding="utf-8")
    accents = {
        name
        for name in re.findall(r"--([a-z0-9-]+):\s*#", css)
        if name in {"resolved", "exception"}
    }
    assert accents == {"resolved", "exception"}


def test_numbers_use_tabular_numerals_and_money_is_right_aligned() -> None:
    css = (REPO_ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert "tabular-nums" in css
    assert re.search(r"\.num\s*\{[^}]*text-align:\s*right", css), "money column is not right-aligned"


# --- integration --------------------------------------------------------------------------


def test_renders_the_real_run(tmp_path: Path) -> None:
    """End-to-end against the committed datasets. Skips when they have not been generated."""
    from eval.provenance import GENERATED_DIR, dataset_files

    if not dataset_files("dev_seed_11", generated_dir=GENERATED_DIR):
        pytest.skip("datasets not generated; run `make seed`")

    out = tmp_path / "index.html"
    assert render_report.main(["--out", str(out)]) == 0

    page = out.read_text(encoding="utf-8")
    assert len(re.findall(r'<div class="rung', page)) == 5
    assert not re.search(r"\bsrc\s*=", page)
    assert len(re.findall(r"<script", page)) == 1

    data = json.loads(
        re.search(
            r'<script id="run-data" type="application/json">(.*?)</script>', page, re.S
        ).group(1).replace("<\\/", "</")
    )
    # Every exception in the payload is rendered — a page showing a subset of the queue while
    # claiming to be the queue is the failure this guards.
    for item in data["exception_items"]:
        assert f'id="{item["id"]}"' in page
