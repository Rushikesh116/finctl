"""Assemble the single page. `make report` writes it to `docs/index.html`.

**No server, no fetch, no build step, and no script.** The whole UI is server-rendered HTML with
the stylesheet inlined, so it works from `file://`, from GitHub Pages, and from inside the
container, with JavaScript disabled. Expansion is native `<details>`/`<summary>`: keyboard
operation, `Enter`/`Space`, and expanded-state semantics come from the element rather than from
code that could break.

The same assembler serves the live app (`api/main.py::index`), so there is one UI and not a live
copy drifting from a static one. `web/page.html` holds the skeleton and `web/app.css` the
styling; both are real files for editing and are inlined here. Token substitution rather than
`str.format`, because CSS is full of braces.

Money is formatted from integer paise **here and nowhere else** — this module is the render
boundary that invariant 1 reserves for it.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from core.money import format_rupees

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"
DEFAULT_OUT = REPO_ROOT / "docs" / "index.html"

PHASE = 6

TITLE = "FinCtl — reconciliation run"

# What a reader sees, not how the system is built. `AMBIGUOUS` needs two entries because it means
# genuinely different things at different layers: Layer 2 cannot tell which *subset of rows*
# settled in a batch, Layer 3 cannot tell which *record* pairs with which. One label for both
# would describe neither.
_LABELS: dict[tuple[str, int | None], str] = {
    ("AMBIGUOUS", 2): "Several row subsets close the gap equally well",
    ("AMBIGUOUS", 3): "Two orders, two payments, nothing tells them apart",
    ("AMBIGUOUS", None): "More than one reading fits equally well",
    ("MISSING_BANK_ROW", None): "Settled at the gateway, no bank credit found",
    ("MISSING_GATEWAY_ROW", None): "Bank credit with no gateway batch behind it",
    ("DUPLICATE_REFERENCE", None): "Reference reused, nothing distinguishes the two",
    ("UNEXPLAINED_ADJ", None): "Adjustment with no order or payment behind it",
    ("SUBSET_SEARCH_EXHAUSTED", None): "Search hit its bound before finding an answer",
    ("TIMING_OUTSIDE_WINDOW", None): "Settles after this period closes",
    ("FX_UNRESOLVED", None): "Multi-currency line whose conversion cannot be reproduced",
    ("DISPUTE_UNRESOLVED", None): "Dispute leg with no matching counter-leg",
    ("ON_HOLD_UNRELEASED", None): "Held balance with no release observed",
    ("VERIFIER_REJECTED", None): "A proposal failed the independent arithmetic re-check",
    ("UNPARSEABLE_NARRATION", None): "Credit is there, its narration cannot be read",
    ("UNCLASSIFIED", None): "Not yet classified — a finding, not a category",
}

# Reader-facing names for the layers, with the mechanism as the sub-line.
_LAYERS: dict[int, tuple[str, str]] = {
    1: ("Matched on reference", "exact identity"),
    2: ("Rebuilt the batch", "net settlement reconstruction"),
    3: ("Best global assignment", "one-to-one, globally optimal"),
    4: ("Read the narration", "model proposes, verifier approves"),
}

_SOURCE_LABEL = {"ledger": "Merchant ledger", "gateway": "Gateway", "bank": "Bank statement"}


def _rupees(paise: int) -> str:
    return format_rupees(paise, prefix="Rs ")


def _when(record: dict) -> str:
    """A readable instant, in IST because that is the timezone the statements are in.

    A bank row already carries its IST value date as a string; gateway and ledger rows carry a UTC
    epoch. Printing the epoch was the first version, and `1773133200` beside an amount tells an
    operator comparing two records nothing at all — the whole reason these rows are shown is so a
    human can see for themselves that two candidates are indistinguishable.
    """
    if record.get("value_date_ist"):
        return f"{record['value_date_ist']} IST"

    epoch = record.get("at_utc")
    if not isinstance(epoch, int):
        return ""

    from core.normalize import ist_date_of

    # Integer arithmetic on the offset rather than a tzdata lookup: IST is a fixed +05:30 with no
    # DST, and `ist_date_of` already owns the date-boundary rule this has to agree with.
    ist_seconds = epoch + 19_800
    return f"{ist_date_of(epoch)} {ist_seconds // 3600 % 24:02d}:{ist_seconds // 60 % 60:02d} IST"


def _pct(numerator: int, denominator: int, places: int = 1) -> str:
    return f"{100 * numerator / denominator:.{places}f}%" if denominator else "n/a"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def label_for(exception_type: str, layer: int | None = None) -> str:
    """Operator-facing wording, most specific match first."""
    if (exception_type, layer) in _LABELS:
        return _LABELS[(exception_type, layer)]
    if (exception_type, None) in _LABELS:
        return _LABELS[(exception_type, None)]
    return exception_type


# --- pieces -------------------------------------------------------------------------------


def _strip(run: dict) -> str:
    n = run["records"]
    matched = run["auto_matched"]
    false_matches = run["false_matches"]
    llm = run["llm"]

    cells: list[tuple[str, str, str, str]] = [
        ("Records", f"{n:,}", "across three sources", ""),
        ("Auto-matched", _pct(matched, n), f"{matched:,} of {n:,} records", "good"),
        (
            "False matches",
            _pct(false_matches, matched, 2),
            f"{false_matches:,} of {matched:,} matched",
            "good" if false_matches == 0 else "bad",
        ),
        (
            "Could not match",
            f"{run['exceptions']:,}",
            f"{_pct(run['exceptions'], n)} of records",
            "bad",
        ),
        ("Amount at risk", _rupees(run["at_risk_paise"]), "in the queue below", "bad"),
        (
            "Model calls",
            f"{llm['calls']:,}",
            f"{100 * llm['calls'] / n if n else 0:.2f} per 100 records",
            "",
        ),
    ]
    return "".join(
        f'<div class="cell {klass}"><dt>{_esc(name)}</dt>'
        f'<dd>{_esc(value)}<span class="sub">{_esc(sub)}</span></dd></div>'
        for name, value, sub, klass in cells
    )


def _cascade(run: dict) -> str:
    n = run["records"]
    if not n:
        return (
            '<div class="empty"><strong>No run to show.</strong>'
            "The datasets have not been generated yet, so there is nothing to reconcile. Run "
            "<code>make demo</code> to build them and produce a run.</div>"
        )

    entering = {int(k): v for k, v in run["entering"].items()}
    resolved = {int(k): v for k, v in run["per_layer"].items()}

    rungs = []
    for layer in sorted(_LAYERS):
        arrived = entering.get(layer, 0)
        settled = resolved.get(layer, 0)
        name, mechanism = _LAYERS[layer]
        # A layer that resolved nothing is marked, not hidden. On this dataset Layer 3 resolves
        # zero, and a bar rendered identically to a productive one would misrepresent that.
        dead = ' dead' if settled == 0 else ""
        detail = f"settled {settled:,}" if settled else "settled none of them"
        rungs.append(
            f'<div class="rung{dead}">'
            f'<span class="lbl">{_esc(name)}<small>{_esc(mechanism)}</small></span>'
            f'<span class="track">'
            f'<span class="fill" style="width:{100 * arrived / n:.2f}%"></span>'
            f'<span class="qty">{arrived:,} arrived <span class="of">&middot; '
            f'{_esc(detail)}</span></span>'
            f"</span></div>"
        )

    unresolved = run["exceptions"]
    rungs.append(
        f'<div class="rung out">'
        f'<span class="lbl">Could not match<small>declined, with a reason</small></span>'
        f'<span class="track">'
        f'<span class="fill" style="width:{100 * unresolved / n:.2f}%"></span>'
        f'<span class="qty">{unresolved:,} records <span class="of">&middot; '
        f"{_pct(unresolved, n)} of the run</span></span>"
        f"</span></div>"
    )
    return "".join(rungs)


def _by_type(run: dict) -> str:
    by_type = run["by_type"]
    if not by_type:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(label_for(kind))}</td>"
        f'<td class="num">{count:,}</td>'
        f'<td class="num">{_esc(_pct(count, sum(by_type.values())))}</td></tr>'
        for kind, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return (
        '<table class="by-type"><thead><tr><th>Reason</th>'
        '<th class="num">Records</th><th class="num">Share</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _record_row(row_id: str, records: dict) -> str:
    """One evidence record, with the keys that would have identified it."""
    record = records.get(row_id)
    if record is None:
        return (
            f'<tr><td class="rid">{_esc(row_id)}</td><td colspan="3" class="keys">'
            "not in the digest &mdash; this record is not named by any exception</td></tr>"
        )

    keys = record.get("keys") or {}
    shown = "".join(
        f"<span>{_esc(name)} {_esc(value)}</span>"
        for name, value in keys.items()
        if value not in (None, "")
    ) or '<span class="keys">no identifying key on this row</span>'

    when = _when(record)
    amount = record.get("amount_paise")
    return (
        f'<tr><td class="rid">{_esc(row_id)}</td>'
        f"<td>{_esc(_SOURCE_LABEL.get(str(record.get('source')), record.get('source')))}"
        f'<br><span class="keys">{_esc(record.get("kind", ""))}</span></td>'
        f'<td class="num">{_esc(_rupees(amount)) if isinstance(amount, int) else ""}'
        f'<br><span class="keys">{_esc(when)}</span></td>'
        f'<td class="keys">{shown}</td></tr>'
    )


def _pairings(item: dict, records: dict) -> str:
    """Layer 3 evidence: every candidate pairing in the tie.

    Rendered as pairs rather than a flat row list because the *pairing* is the claim being
    refused. Four rows reading `ml_000222 gw_000295` … tell a reader nothing on their own; four
    labelled pairs, each with both records' amounts and keys, let them confirm for themselves that
    nothing distinguishes the options.
    """
    blocks = []
    for index, evidence in enumerate(item["evidence"], 1):
        rows = "".join(_record_row(row_id, records) for row_id in evidence["row_ids"])
        blocks.append(
            f'<tr class="pair"><td colspan="4" class="keys">Pairing {index} '
            f'&mdash; <span class="pairsum">both sides {_esc(_rupees(evidence["sum_paise"]))}'
            f"</span></td></tr>{rows}"
        )
    return (
        '<div class="ev-wrap"><table class="ev"><thead><tr><th>Record</th><th>Source</th>'
        '<th class="num">Amount</th><th>Keys</th></tr></thead>'
        f"<tbody>{''.join(blocks)}</tbody></table></div>"
    )


def _subsets(item: dict, records: dict) -> str:
    """Layer 2 evidence: the subsets that each close δ.

    Row ids plus the sum, and the sum is printed because a reader must be able to check it equals
    δ without joining back to anything.
    """
    lines = []
    for index, evidence in enumerate(item["evidence"], 1):
        ids = " + ".join(_esc(row_id) for row_id in evidence["row_ids"])
        lines.append(
            f'<p class="subset"><span class="eq">Subset {index} of '
            f'{item["evidence_found"]}:</span> <span class="ids">{ids}</span> '
            f'<span class="eq">=</span> <span class="pairsum">'
            f'{_esc(_rupees(evidence["sum_paise"]))}</span></p>'
        )

    referenced = sorted({row_id for e in item["evidence"] for row_id in e["row_ids"]})
    rows = "".join(_record_row(row_id, records) for row_id in referenced)
    table = (
        '<div class="ev-wrap"><table class="ev"><thead><tr><th>Record</th><th>Source</th>'
        '<th class="num">Amount</th><th>Keys</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        if rows
        else ""
    )
    return "".join(lines) + table


def _named_records(item: dict, records: dict) -> str:
    """The records this exception is *about*.

    Shown for every exception, not only the ones with competing candidates. Where a refusal has no
    alternatives to weigh — a settlement with no matching bank credit, say — these rows are the
    entire basis for it, and the first version of this page omitted them, so those exceptions
    expanded into prose and a hash with nothing an operator could act on.
    """
    row_ids = item["record_ids"]
    if not row_ids:
        return ""

    rows = "".join(_record_row(row_id, records) for row_id in row_ids)
    return (
        f'<h3>Records named <span class="ok">{len(row_ids)}</span></h3>'
        '<div class="ev-wrap"><table class="ev"><thead><tr><th>Record</th><th>Source</th>'
        '<th class="num">Amount</th><th>Keys</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _evidence(item: dict, records: dict) -> str:
    found = item["evidence_found"]
    shown = len(item["evidence"])

    if not shown:
        # Three genuinely different silences, and collapsing them would be the same error as
        # conflating "declined" with "gave up". `complete=False` means the count is a floor.
        if not item["evidence_complete"]:
            return (
                '<h3>Evidence <span class="flag">none recorded &mdash; count is a lower '
                "bound</span></h3>"
                '<p class="note">The search stopped at its bound before finding any subset that '
                "closes the gap, so no candidate can be shown and the true number of subsets is "
                "unknown. This is the stopping rule firing, not a claim that none exists.</p>"
            )
        return (
            '<h3>Evidence <span class="ok">the records above</span></h3>'
            '<p class="note">Nothing to choose between: this reason is established by the records '
            "named above rather than by competing candidates.</p>"
        )

    flag = (
        f'<span class="flag">showing {shown} of {found} &mdash; truncated</span>'
        if item["evidence_truncated"]
        else f'<span class="ok">all {found} shown</span>'
    )
    lower_bound = (
        '<p class="note">The recorded count is a lower bound: the search was stopped before '
        "enumerating every possibility.</p>"
        if not item["evidence_complete"]
        else ""
    )

    # Layer 3 records pairings, Layer 2 records subsets. Same field, different claim.
    body = _pairings(item, records) if item["layer"] == 3 else _subsets(item, records)
    return f"<h3>Evidence {flag}</h3>{lower_bound}{body}"


def _trail(item: dict) -> str:
    if not item["audit_trail"]:
        return (
            "<h3>Audit trail</h3>"
            '<p class="note">No ledger entry names these records. Run <code>make run</code> to '
            "write the audit log.</p>"
        )
    rows = "".join(
        f'<tr><td class="num">{entry["seq"]}</td><td>L{entry["layer"]}</td>'
        f'<td>{_esc(entry["decision"])}</td><td>{_esc(entry["outcome"])}</td>'
        f'<td class="hash">{_esc(entry["entry_hash"])}</td>'
        f'<td class="det">{_esc(entry["detail"])}'
        + (
            f'<br><span class="keys">{_esc(entry["provider"])} '
            f'{_esc(entry["model"] or "")}</span>'
            if entry.get("provider")
            else ""
        )
        + "</td></tr>"
        for entry in item["audit_trail"]
    )
    return (
        "<h3>Audit trail <span class=\"ok\">hash-chained, replayable</span></h3>"
        '<div class="trail-wrap"><table class="trail"><thead><tr><th class="num">#</th>'
        "<th>Layer</th><th>Decision</th><th>Outcome</th><th>Entry hash</th><th>Detail</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _queue(run: dict) -> str:
    items = run["exception_items"]
    if not items:
        # Terminal states get a sentence explaining what the emptiness means, not just the fact of
        # it. "Nothing here" leaves a reader unsure whether the queue is clear or the run failed.
        return (
            '<div class="empty"><strong>Nothing to review.</strong>'
            "Every record was matched to a counterpart and each group balanced against the bank "
            "to the paisa. That is the intended end state, not an error &mdash; but if it is "
            "unexpected for this dataset, run <code>make eval</code> and check the false-match "
            "rate before trusting it.</div>"
        )

    records = run["record_digest"]
    blocks = []
    for item in items:
        label = label_for(item["type"], item["layer"])
        blocks.append(
            f'<details class="item" id="{_esc(item["id"])}">'
            f'<summary><span class="mark" aria-hidden="true"></span>'
            f'<span class="what">{_esc(label)}'
            f'<span class="where">found at layer {item["layer"]} '
            f'&middot; {_esc(item["type"])}</span></span>'
            f'<span class="cnt num">{len(item["record_ids"]):,} recs</span>'
            f'<span class="amt num">{_esc(_rupees(item["amount_at_risk_paise"]))}</span>'
            f"</summary>"
            f'<div class="body">'
            f"<p>{_esc(item['detail'])}</p>"
            f"{_named_records(item, records)}"
            f"{_evidence(item, records)}"
            f"{_trail(item)}"
            f"</div></details>"
        )
    return "".join(blocks)


def _intro(run: dict) -> str:
    """Explain the problem to someone who has never seen this page.

    Plain language throughout: no δ, no netting, no subset-sum, no cascade. A reader who does not
    already know what a settlement batch is should be able to finish this block understanding why
    a reference lookup cannot solve it.

    Every figure is read from the run rather than written into the sentence, so the prose cannot
    drift from the dataset it is describing.
    """
    counts = run.get("source_counts") or {}
    sales = counts.get("merchant_sales")
    credits = counts.get("bank_credits")
    example = run.get("worked_example")

    parts: list[str] = []

    if sales and credits:
        parts.append(
            f'<p class="lede">This shop made <strong>{sales:,} sales</strong> in a month. '
            f"Its bank statement for the same month has <strong>{credits} credits</strong> on "
            f"it.</p>"
        )
        parts.append(
            "<p>That gap is the whole problem. The payment company does not pay out one sale at "
            "a time. It gathers up a batch, subtracts its own fee and the tax on that fee, "
            "subtracts anything that was refunded, and sends a single amount. So one line on the "
            "bank statement is dozens of sales added together and then reduced by charges that "
            "are written down nowhere on that line.</p>"
        )
    parts.append(
        "<p>Checking one of those credits is therefore not a lookup. There is no reference number "
        "that leads from the bank line back to the sales inside it. The question is "
        "<strong>which set of transactions adds up to exactly this amount</strong> — and you "
        "cannot answer it by matching one row to one row.</p>"
    )

    if example:
        joined_share = (
            100 * int(example["joined_paise"]) / int(example["credit_paise"])
            if example["credit_paise"]
            else 0
        )
        parts.append(
            f"<p>Here is a real one from this run, the payout dated "
            f"{_esc(example['value_date_ist'])}:</p>"
        )
        parts.append(
            f'<div class="worked"><dl>'
            f"<dt>What the bank shows<small>one credit, one line</small></dt>"
            f"<dd>{_esc(_rupees(int(example['credit_paise'])))}</dd>"
            f"<dt>Transactions that name this payout"
            f"<small>{example['joined_rows']} rows, found by their reference</small></dt>"
            f"<dd>{_esc(_rupees(int(example['joined_paise'])))}</dd>"
            f'<dt class="gap">Left unexplained'
            f"<small>only {joined_share:.0f}% of the credit accounted for so far</small></dt>"
            f'<dd class="gap">{_esc(_rupees(int(example["searched_paise"])))}</dd>'
            f'<dt class="closed">Transactions that close it exactly'
            f"<small>{example['searched_rows']} rows, and nothing links them to this payout"
            f"</small></dt>"
            f'<dd class="closed">{_esc(_rupees(int(example["searched_paise"])))}</dd>'
            f"</dl></div>"
        )
        parts.append(
            f"<p>Those last {example['searched_rows']} rows carry no reference, no batch number, "
            f"nothing at all tying them to this payout. They were found by searching for a "
            f"combination that closes the gap to the paisa, and they were only accepted after the "
            f"total was recomputed against the bank figure and matched exactly. A lookup finds "
            f"{example['joined_rows']} of the {int(example['joined_rows']) + int(example['searched_rows'])} "
            f"rows behind this credit. The rest have to be worked out.</p>"
        )

    parts.append(
        "<p>FinCtl does this for every credit on the statement, and where the records genuinely "
        "do not settle the question, it says so rather than guessing. "
        "<strong>Click any row in the list below to see the records and the reasoning behind that "
        "decision.</strong></p>"
    )
    return "".join(parts)


def _banner(run: dict) -> str:
    prov = run["provenance"]
    llm = run["llm"]
    notes = []
    if prov["manifest_state"] == "drift":
        notes.append(
            "Dataset drift: the files on disk do not match the committed manifest, so these "
            "numbers describe data that is not the data in the repository. Run "
            "<code>make seed</code>."
        )
    elif prov["manifest_state"] == "absent":
        notes.append("No dataset manifest. Run <code>make seed</code>.")
    if llm["stubbed"]:
        notes.append(
            "Mixed adjudication: some Layer 4 responses in this run came from an offline stub "
            "rather than a model. The split, and why, is in <code>docs/METRICS.md</code>."
        )
    if not notes:
        return ""
    return "".join(f'<p class="warn">{note}</p>' for note in notes)


# --- assembly -----------------------------------------------------------------------------


def build_html(run: dict) -> str:
    """Render the whole page. Raises `FileNotFoundError` if `web/` is missing."""
    template = (WEB_DIR / "page.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    prov = run["provenance"]
    llm = run["llm"]
    n = run["records"]

    provenance = " ".join(
        [
            f"<span><b>Dataset</b> <span class='v'>{_esc(run['dataset'])}</span></span>",
            f"<span><b>Data</b> <span class='v'>{_esc(prov['dataset_sha'])}</span></span>",
            f"<span><b>Code</b> <span class='v'>{_esc(prov['git_sha'])}</span></span>",
            f"<span><b>Run</b> <span class='v'>{_esc(prov['started_at_utc'])} UTC</span></span>",
            f"<span><b>Wall clock</b> <span class='v'>{run['wall_clock_ms']:,} ms</span></span>",
            f"<span><b>Adjudicator</b> {_esc(llm['provider'])}"
            + (f" {_esc(', '.join(llm['model_versions']))}" if llm["model_versions"] else "")
            + f" ({_esc(llm['mode'])})</span>",
            f"<span><b>Ledger</b> {run['ledger']['entries']:,} entries, head "
            f"<span class='v'>{_esc(run['ledger']['head'][:12])}</span></span>",
        ]
    )

    absent = run["by_class"].get("absent", 0)
    undetermined = run["by_class"].get("undetermined", 0)
    matched = run["auto_matched"]
    items = len(run["exception_items"])

    replacements = {
        "%%TITLE%%": _esc(TITLE),
        "%%CSS%%": css,
        "%%H1%%": _esc("Payment reconciliation"),
        "%%TAGLINE%%": _esc(
            "Three records of the same money — what the shop sold, what the payment company "
            "processed, and what reached the bank — checked against each other, with everything "
            "that did not line up listed and explained."
        ),
        "%%PROVENANCE%%": provenance,
        "%%BANNER%%": _banner(run),
        # --- intro ---
        "%%INTRO_H%%": _esc("What this page is"),
        "%%INTRO_SAYS%%": _esc(
            "Why matching a bank credit to the sales behind it is harder than looking up a "
            "reference number."
        ),
        "%%INTRO%%": _intro(run),
        # --- strip ---
        "%%STRIP_H%%": _esc("The result"),
        "%%STRIP_SAYS%%": _esc(
            f"How much of this run FinCtl settled on its own, and how much it got wrong — "
            f"measured against known answers it never reads while matching."
        ),
        "%%STRIP%%": _strip(run),
        # --- cascade ---
        "%%CASCADE_H%%": _esc("How the work was done"),
        "%%CASCADE_SAYS%%": _esc(
            "Four methods, tried in order, each one handed only what the methods before it could "
            "not settle."
        ),
        "%%CASCADE%%": _cascade(run),
        "%%CASCADE_CAPTION%%": _esc(
            "Each bar is how many records arrived at that step, so the bars narrow as the work "
            "gets done. A bar the same width as the one above it means that step settled nothing "
            "on this run — which is a fact about the method, not a drawing error."
        ),
        # --- queue ---
        "%%QUEUE_H%%": _esc("What could not be matched"),
        "%%QUEUE_SAYS%%": _esc(
            f"{items} findings covering {run['exceptions']:,} records, worth "
            f"{_rupees(run['at_risk_paise'])}, ordered with the most money first."
        ),
        "%%BY_TYPE%%": _by_type(run),
        "%%QUEUE%%": _queue(run),
        "%%QUEUE_CAPTION%%": _esc(
            f"Click a row to open it. Refusing to answer is the right outcome where the records "
            f"do not settle the question: {absent} of these have no counterpart in the data at "
            f"all, and {undetermined} have one that cannot be told apart from another. Each open "
            f"row shows the records involved, the alternatives that were weighed, and the audit "
            f"entries written at the time."
        ),
        # --- raw ---
        "%%RAW_H%%": _esc("The underlying numbers"),
        "%%RAW_SAYS%%": _esc(
            "The same run as printed by the command line, for anyone who wants to check the "
            "figures above against their source."
        ),
        "%%BLOCK_SUMMARY%%": _esc("Full metrics, exactly as make eval prints them"),
        "%%ABLATION_SUMMARY%%": _esc(
            "Each method measured on its own — every row is a real run, not a subtraction"
        ),
        "%%BLOCK%%": _esc(run["block"]) or "Run make eval.",
        "%%ABLATION%%": _esc(run["ablation"]) or "Run make eval.",
        "%%FOOTER%%": (
            f"{matched:,} of {n:,} records matched in {run['wall_clock_ms']:,} ms. "
            f"Amounts are held as whole paise throughout and turned into rupees only here, at the "
            f"point of display. Nothing on this page runs, and it requests nothing from the "
            f"network."
        ),
        # `</` is the only sequence that can terminate a script element early, so escaping it is
        # what keeps inlined data from being able to break out of the block.
        "%%DATA%%": json.dumps(run, indent=2, sort_keys=True, default=str).replace("</", "<\\/"),
    }

    page = template
    for token, value in replacements.items():
        page = page.replace(token, value)

    leftover = [token for token in replacements if token in page]
    if leftover:  # pragma: no cover - guards against a token renamed in only one file
        raise RuntimeError(f"template tokens not substituted: {leftover}")
    return page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render the static run report")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dataset", default="dev_seed_11")
    args = parser.parse_args(argv)

    from api.main import compute_run

    run = compute_run(args.dataset)
    # Resolve before use: `--out docs/index.html` is relative, and reporting it against the repo
    # root without resolving raised rather than printing.
    out = args.out if args.out.is_absolute() else (Path.cwd() / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(run), encoding="utf-8")

    try:
        shown = out.relative_to(REPO_ROOT)
    except ValueError:
        shown = out
    print(f"wrote {shown} ({out.stat().st_size:,} bytes, inlined, no script, no fetch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
