"""Render the run to a static `docs/index.html`. `make report`.

**No server, no fetch, no build step.** The data is inlined as JSON in a `<script type="application
/json">` block and read from the DOM, so the file works from `file://`, from GitHub Pages, and
from inside the container. That is the zero-infrastructure fallback: if the live service is
asleep, the numbers are still readable.

This is the Block 3 report — correct, legible, and deliberately plain. The designed single page
with the cascade as proportional bars is Block 4; the gate for that is both URLs responding first,
so this renders the same data without spending time on visual polish that would be redone.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "docs" / "index.html"

PHASE = 6

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FinCtl — reconciliation run</title>
<style>
  :root {{
    --ink: #16181d; --muted: #6b7280; --rule: #e5e7eb; --bg: #ffffff;
    --resolved: #1f6f4a; --exception: #a5401a;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --ink: #e8eaed; --muted: #9aa0a6; --rule: #2c2f35; --bg: #14161a;
      --resolved: #4fae82; --exception: #d97a51;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2.5rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
    font: 400 15px/1.55 ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }}
  main {{ max-width: 62rem; margin: 0 auto; }}
  h1 {{ font-size: 1.1rem; font-weight: 600; margin: 0 0 .25rem; letter-spacing: -0.01em; }}
  .run {{ color: var(--muted); font-size: .8rem; margin-bottom: 2.25rem; }}
  h2 {{
    font-size: .75rem; font-weight: 600; text-transform: none; color: var(--muted);
    margin: 2.5rem 0 .75rem; padding-bottom: .35rem; border-bottom: 1px solid var(--rule);
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
  th {{ text-align: left; font-weight: 500; color: var(--muted); padding: .3rem 0; }}
  td {{ padding: .3rem 0; border-top: 1px solid var(--rule); }}
  td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .cascade div {{ margin: .3rem 0; display: flex; align-items: center; gap: .6rem; }}
  .cascade .bar {{
    height: .7rem; background: var(--resolved); border-radius: 1px; min-width: 1px;
  }}
  .cascade .lbl {{ width: 11rem; flex: none; }}
  .cascade .val {{ color: var(--muted); font-size: .78rem; }}
  .ex .bar {{ background: var(--exception); }}
  pre {{
    background: transparent; border: 1px solid var(--rule); border-radius: 2px;
    padding: .9rem 1rem; overflow-x: auto; font-size: .76rem; line-height: 1.5;
  }}
  .note {{ color: var(--muted); font-size: .78rem; margin: .6rem 0 0; }}
  a {{ color: inherit; }}
  a:focus-visible, :focus-visible {{ outline: 2px solid currentColor; outline-offset: 2px; }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
</head>
<body>
<main>
  <h1>FinCtl — payment reconciliation</h1>
  <p class="run">{header}</p>

  <h2>The cascade</h2>
  <div class="cascade">{cascade}</div>
  <p class="note">{cascade_note}</p>

  <h2>Measured</h2>
  <table>
    <tbody>{metrics}</tbody>
  </table>

  <h2>Could not match — {exceptions_total} records</h2>
  <table>
    <thead><tr><th>Reason</th><th class="n">Records</th></tr></thead>
    <tbody>{exceptions}</tbody>
  </table>
  <p class="note">{exceptions_note}</p>

  <h2>Full metrics block, as printed by <code>make eval</code></h2>
  <pre>{block}</pre>

  <h2>Ablation — each arm is a real run on the same dataset</h2>
  <pre>{ablation}</pre>

  <p class="note">{footer}</p>
</main>
<script id="run-data" type="application/json">{data}</script>
</body>
</html>
"""

# Operator-facing wording. The exception TYPE is how the system thinks; these are what a person
# reading the queue needs to see, per the UI rules in the brief.
_LABELS = {
    "AMBIGUOUS": "Two or more candidates fit equally — declined on purpose",
    "MISSING_BANK_ROW": "Settled at the gateway, no bank credit found",
    "MISSING_GATEWAY_ROW": "Bank credit with no gateway batch behind it",
    "DUPLICATE_REFERENCE": "Reference reused, nothing distinguishes the two",
    "UNEXPLAINED_ADJ": "Adjustment with no order or payment reference",
    "SUBSET_SEARCH_EXHAUSTED": "Search hit its bound before finding an answer",
    "TIMING_OUTSIDE_WINDOW": "Settles outside this period — pending writeback",
    "FX_UNRESOLVED": "Multi-currency line whose conversion cannot be reproduced",
    "DISPUTE_UNRESOLVED": "Dispute leg with no matching counter-leg",
    "ON_HOLD_UNRELEASED": "Held balance with no observed release",
    "VERIFIER_REJECTED": "A proposal failed the independent arithmetic re-check",
    "UNPARSEABLE_NARRATION": "Credit exists, its narration cannot be read",
    "UNCLASSIFIED": "Not yet classified — a finding, not a category",
}

_LAYERS = {"1": "1 · exact", "2": "2 · netting", "3": "3 · fuzzy", "4": "4 · adjudicated"}


def _rupees(paise: int) -> str:
    from core.money import format_rupees

    return format_rupees(paise, prefix="Rs ")


def _pct(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "n/a"


def build_html(run: dict) -> str:
    n = run["records"]
    prov = run["provenance"]

    header = html.escape(
        f"{run['dataset']} · data {prov['dataset_sha']} · code {prov['git_sha']} · "
        f"{prov['started_at_utc']} · {run['wall_clock_ms']} ms"
    )

    # The cascade rendered as data: bar width is the share of records resolved at that layer.
    bars = []
    for layer, count in run["per_layer"].items():
        width = 100 * count / n if n else 0
        bars.append(
            f'<div><span class="lbl">Layer {html.escape(_LAYERS.get(layer, layer))}</span>'
            f'<span class="bar" style="width:{width:.2f}%"></span>'
            f'<span class="val">{count} · {_pct(count, n)}</span></div>'
        )
    unresolved = run["exceptions"]
    bars.append(
        f'<div class="ex"><span class="lbl">Could not match</span>'
        f'<span class="bar" style="width:{100 * unresolved / n if n else 0:.2f}%"></span>'
        f'<span class="val">{unresolved} · {_pct(unresolved, n)}</span></div>'
    )

    rows = [
        ("Records processed", f"{n}", ""),
        ("Auto-matched", f"{run['auto_matched']}", _pct(run["auto_matched"], n)),
        (
            "False matches — precision, not coverage",
            f"{run['false_matches']}",
            _pct(run["false_matches"], run["auto_matched"]),
        ),
        ("Could not match", f"{run['exceptions']}", _pct(run["exceptions"], n)),
        (
            "  of which correctly declined",
            f"{run['correctly_flagged']}",
            _pct(run["correctly_flagged"], run["exceptions"]),
        ),
        ("  of which missed", f"{run['missed_matches']}", _pct(run["missed_matches"], run["exceptions"])),
        ("Amount at risk", _rupees(run["at_risk_paise"]), ""),
        ("Model calls", f"{run['llm']['calls']}", f"{100 * run['llm']['calls'] / n if n else 0:.2f} / 100"),
        (
            "Rules learned from narration",
            f"{run['llm']['rules_promoted']}",
            f"of {run['llm']['rules_total']} cached",
        ),
        ("Audit ledger", f"{run['ledger']['entries']} entries", run["ledger"]["head"][:12]),
    ]
    metrics = "".join(
        f"<tr><td>{html.escape(label)}</td><td class='n'>{html.escape(value)}</td>"
        f"<td class='n'>{html.escape(extra)}</td></tr>"
        for label, value, extra in rows
    )

    exceptions = "".join(
        f"<tr><td>{html.escape(_LABELS.get(kind, kind))}</td><td class='n'>{count}</td></tr>"
        for kind, count in sorted(run["by_type"].items(), key=lambda kv: -kv[1])
    )

    llm = run["llm"]
    stub_note = (
        " Adjudication responses in this run came from an offline stub, not a model — see "
        "docs/METRICS.md."
        if llm["stubbed"]
        else ""
    )
    footer = html.escape(
        f"Provenance {prov['manifest_state']}; adjudicator {llm['provider']} "
        f"({llm['mode']} mode).{stub_note} This page has data inlined and makes no network "
        f"requests."
    )

    return _TEMPLATE.format(
        header=header,
        cascade="".join(bars),
        cascade_note=html.escape(
            "Bar width is the share of all records resolved at that layer. Each layer sees only "
            "what the layers above it could not settle."
        ),
        metrics=metrics,
        exceptions_total=run["exceptions"],
        exceptions=exceptions,
        exceptions_note=html.escape(
            "Declining is a success where the data does not determine an answer. "
            f"{run['by_class'].get('absent', 0)} of these have no counterpart in the data at all; "
            f"{run['by_class'].get('undetermined', 0)} have one that cannot be identified."
        ),
        block=html.escape(run["block"]),
        ablation=html.escape(run["ablation"]),
        footer=footer,
        data=json.dumps(run, indent=2, sort_keys=True).replace("</", "<\\/"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render the static run report")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dataset", default="dev_seed_11")
    args = parser.parse_args(argv)

    from api.main import compute_run

    run = compute_run(args.dataset)
    # Resolve before use: `--out docs/index.html` is a relative path, and reporting it against
    # the repo root without resolving raised rather than printing.
    out = args.out if args.out.is_absolute() else (Path.cwd() / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(run), encoding="utf-8")

    try:
        shown = out.relative_to(REPO_ROOT)
    except ValueError:
        shown = out
    print(f"wrote {shown} ({out.stat().st_size:,} bytes, data inlined, no fetch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
