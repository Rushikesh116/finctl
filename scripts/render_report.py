"""Assemble the report. `make report` writes five real files into `docs/`.

**No server, no fetch, no build step, and no script.** The whole UI is server-rendered HTML with
the stylesheet inlined, so it works from `file://`, from GitHub Pages, and from inside the
container, with JavaScript disabled. Expansion is native `<details>`/`<summary>`: keyboard
operation, `Enter`/`Space`, and expanded-state semantics come from the element rather than from
code that could break. Filtering on the exceptions page is CSS `:target` for the same reason —
see D-0026.

**One assembler, parameterised by link style** (D-0026). The live app (`api/main.py`) renders the
same pages at routes `/`, `/run`, `/exceptions`, `/settings`, `/about`; the static export renders
them as `index.html`, `run.html`, … with relative links. The *only* difference between the two is
how a link is spelled, so a link map is the whole of the abstraction. There is no second renderer
and therefore no way for the published page to drift from the live one.

`web/page.html` holds the shell and `web/app.css` the styling; both are real files for editing and
are inlined here. Token substitution rather than `str.format`, because CSS is full of braces.

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
DEFAULT_OUT_DIR = REPO_ROOT / "docs"

PHASE = 6

TITLE = "FinCtl — reconciliation run"

# key -> (static filename, server route, nav label, page title, the one grey sentence)
#
# The nav label is what a reader scanning for a section reads, so it names the *content* rather
# than the machinery: "What could not be matched", not "Exceptions". The internal key stays
# short because it is what the code joins on.
PAGES: dict[str, tuple[str, str, str, str, str]] = {
    "overview": (
        "index.html",
        "/",
        "Overview",
        "Payment reconciliation",
        "Why matching a bank credit to the sales behind it is harder than looking up a "
        "reference number, worked through one real payout from this run.",
    ),
    "run": (
        "run.html",
        "/run",
        "This run",
        "How the run went",
        "What each of the four methods settled, and the full figures exactly as the command "
        "line prints them.",
    ),
    "exceptions": (
        "exceptions.html",
        "/exceptions",
        "What could not be matched",
        "What could not be matched",
        "Every record FinCtl declined to match, why it declined, and the evidence it recorded "
        "at the time.",
    ),
    "settings": (
        "settings.html",
        "/settings",
        "Live data",
        "Reading a real settlement",
        "Point FinCtl at one real test-mode settlement and see the canonical records it "
        "produces. Read-only, and off by default.",
    ),
    "about": (
        "about.html",
        "/about",
        "How it works",
        "How it works, and what it does not do",
        "The four methods, the check that every proposed match has to survive, what broke "
        "while building it, and the limits of these numbers.",
    ),
}

NAV_ORDER = ("overview", "run", "exceptions", "settings", "about")

SERVER_LINKS = {key: PAGES[key][1] for key in PAGES}
"""Routes, for the live FastAPI app."""

STATIC_LINKS = {key: PAGES[key][0] for key in PAGES}
"""Relative filenames, for the static export. Relative because the export is published under a
project subpath on GitHub Pages, where an absolute `/run` would resolve to the domain root."""

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
            f"</span>"
            f'<span class="qty">{arrived:,} arrived <span class="of">&middot; '
            f'{_esc(detail)}</span></span>'
            f"</div>"
        )

    unresolved = run["exceptions"]
    rungs.append(
        f'<div class="rung out">'
        f'<span class="lbl">Could not match<small>declined, with a reason</small></span>'
        f'<span class="track">'
        f'<span class="fill" style="width:{100 * unresolved / n:.2f}%"></span>'
        f"</span>"
        f'<span class="qty">{unresolved:,} records <span class="of">&middot; '
        f"{_pct(unresolved, n)} of the run</span></span>"
        f"</div>"
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
        # The type is carried as a class so the CSS `:target` filter can hide the others without
        # a line of script (D-0026). It is a class, not a data attribute, because attribute
        # selectors and class selectors cost the same here and a class reads plainly in devtools.
        blocks.append(
            f'<details class="item t-{_esc(item["type"])}" id="{_esc(item["id"])}">'
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


def _findings_by_type(run: dict) -> dict[str, int]:
    """How many *rows of the queue* carry each type.

    Deliberately not `run["by_type"]`, which counts **records** and sums to more than the record
    total because one record can be named by several exceptions. Filtering hides and shows rows,
    so the number on a filter control has to be a count of rows or the control lies about what
    pressing it does. The record view is the table directly above, which is labelled `Records`.
    """
    counts: dict[str, int] = {}
    for item in run["exception_items"]:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return counts


def _filter_targets(run: dict) -> str:
    """The empty anchors the CSS `:target` filter keys off.

    One per type actually present in the queue, plus `f-all`. They carry no content and are not
    focusable: their only job is to be what the URL fragment points at, so that a sibling
    combinator can reach the queue and hide the rows that do not match. Rendered before the queue
    because `#f-X:target ~ .card` requires the target to *precede* it.
    """
    keys = ["all", *sorted(_findings_by_type(run))]
    return "".join(f'<i class="ftarget" id="f-{_esc(key)}"></i>' for key in keys)


def _filters(run: dict, *, here: str) -> str:
    """Filter links, each carrying the number of rows it leaves showing.

    Fragment links rather than query strings, so the feature survives in the static export where
    there is no server to read one (D-0026). The count sits on the control because a filter that
    leads somewhere unexpected is worse than no filter, and none of these can lead to an empty
    list: every chip is generated from a type that is actually in the queue.
    """
    counts = _findings_by_type(run)
    if not counts:
        return ""

    links = [
        f'<a class="chip" href="{_esc(here)}#f-all">All findings'
        f'<span class="cnt num">{sum(counts.values()):,}</span></a>'
    ]
    for kind, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        links.append(
            f'<a class="chip" href="{_esc(here)}#f-{_esc(kind)}">{_esc(label_for(kind))}'
            f'<span class="cnt num">{count:,}</span></a>'
        )
    return (
        f'<div class="filters">{"".join(links)}</div>'
        '<p class="caption filter-note">Counts here are findings — rows in the list below. '
        "One finding can name several records, which is why they do not add up to the record "
        "count in the table above.</p>"
    )


def _nav(links: dict[str, str], here: str) -> str:
    """The section links. The current page is marked and not a link to itself.

    `aria-current="page"` rather than only a colour, so the marking survives for a reader who is
    not seeing the colour. A self-link would be a control that does nothing, which is the thing
    `test_navigation_is_honest` exists to forbid.
    """
    parts = []
    for key in NAV_ORDER:
        label = PAGES[key][2]
        if key == here:
            parts.append(f'<span class="here" aria-current="page">{_esc(label)}</span>')
        else:
            parts.append(f'<a href="{_esc(links[key])}">{_esc(label)}</a>')
    return "".join(parts)


def _onward(links: dict[str, str], here: str) -> str:
    """Where to go next, as a sentence rather than a row of buttons.

    Every page ends with one, because a reader who has finished a page and is offered nothing is
    a reader who leaves. The wording names what they would find, not the page's title.
    """
    routes = {
        "overview": (
            f'<a href="{_esc(links["run"])}">See how the run went</a> — what each method '
            f"settled, and the figures behind the headline. Or go straight to "
            f'<a href="{_esc(links["exceptions"])}">what could not be matched</a>.'
        ),
        "run": (
            f'<a href="{_esc(links["exceptions"])}">Read what could not be matched</a> — the '
            f"records behind the exception count above, each with its evidence. "
            f'<a href="{_esc(links["about"])}">How it works</a> explains the four methods.'
        ),
        "exceptions": (
            f'<a href="{_esc(links["about"])}">How it works</a> explains why declining is the '
            f"intended outcome for many of these, and what the arithmetic check guarantees. "
            f'<a href="{_esc(links["run"])}">This run</a> has the figures.'
        ),
        "settings": (
            f'<a href="{_esc(links["overview"])}">Back to the overview</a> for the problem this '
            f'solves, or <a href="{_esc(links["about"])}">how it works</a> for the design.'
        ),
        "about": (
            f'<a href="{_esc(links["overview"])}">Back to the overview</a>, or '
            f'<a href="{_esc(links["exceptions"])}">the exception queue</a> to see the '
            f"refusals in practice."
        ),
    }
    return f'<p class="onward">{routes[here]}</p>'


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




# --- page bodies --------------------------------------------------------------------------
#
# One function per page. Each returns the inside of `<div id="main">`, and each section is a
# `<h2>` followed immediately by the one grey sentence that says what it shows — the pattern the
# reference screenshot uses, and asserted by `test_headings_are_large_and_light_with_a_sentence`.


def _section(heading: str, says: str, body: str, caption: str = "") -> str:
    tail = f'<p class="caption">{_esc(caption)}</p>' if caption else ""
    return (
        f"<section><h2>{_esc(heading)}</h2>"
        f'<p class="says">{_esc(says)}</p>{body}{tail}</section>'
    )


def _page_overview(run: dict, links: dict[str, str]) -> str:
    n = run["records"]
    matched = run["auto_matched"]
    counts = run.get("source_counts") or {}

    headline = (
        '<dl class="strip">'
        f'<div class="cell"><dt>Records</dt><dd>{n:,}'
        f'<span class="sub">across three sources</span></dd></div>'
        f'<div class="cell good"><dt>Matched on its own</dt><dd>{_pct(matched, n)}'
        f'<span class="sub">{matched:,} of {n:,}</span></dd></div>'
        f'<div class="cell good"><dt>Matched wrongly</dt>'
        f'<dd>{_pct(run["false_matches"], matched, 2)}'
        f'<span class="sub">{run["false_matches"]:,} of {matched:,}</span></dd></div>'
        f'<div class="cell bad"><dt>Could not match</dt><dd>{run["exceptions"]:,}'
        f'<span class="sub">{_pct(run["exceptions"], n)} of records</span></dd></div>'
        "</dl>"
    )

    span = ""
    if counts.get("first_value_date"):
        span = (
            f' The statement covers {_esc(counts["first_value_date"])} to '
            f'{_esc(counts["last_value_date"])}.'
        )

    return (
        _section(
            "The problem",
            "One shop, one month, three separate records of the same money — and a bank "
            "statement that does not show individual sales at all.",
            f'<div class="card intro">{_intro(run)}</div>',
        )
        + _section(
            "What happened on this run",
            "The headline figures. Matched wrongly is measured against known answers that the "
            "matcher never reads while it is working." + span,
            headline,
            "Matching more is easy if you are willing to be wrong. Both numbers are shown "
            "together because only the pair means anything.",
        )
        + _section(
            "Where to go next",
            "This report is five pages. Each one answers a different question.",
            '<div class="card">'
            f'<dl class="onward-list">'
            f'<dt><a href="{_esc(links["run"])}">This run</a></dt>'
            f"<dd>What each of the four methods settled, the full metrics block, and each "
            f"method measured on its own.</dd>"
            f'<dt><a href="{_esc(links["exceptions"])}">What could not be matched</a></dt>'
            f'<dd>{run["exceptions"]:,} records FinCtl declined, filterable by reason, each '
            f"opening to the records and evidence behind the decision.</dd>"
            f'<dt><a href="{_esc(links["about"])}">How it works</a></dt>'
            f"<dd>The four methods, the arithmetic check every proposed match must survive, "
            f"what broke while building it, and what these numbers do not cover.</dd>"
            f'<dt><a href="{_esc(links["settings"])}">Live data</a></dt>'
            f"<dd>Read one real test-mode settlement through the ingestion adapter. Off by "
            f"default.</dd>"
            f"</dl></div>",
        )
    )


def _page_run(run: dict, links: dict[str, str]) -> str:
    n = run["records"]
    value = run.get("value") or {}

    value_strip = ""
    if value:
        value_strip = _section(
            "The same run, weighted by money",
            "Records matched is not value matched. A method can settle many small records and "
            "little money, or the reverse, so both are reported.",
            '<dl class="strip">'
            f'<div class="cell"><dt>Value in the run</dt>'
            f'<dd>{_esc(_rupees(value["total_paise"]))}'
            f'<span class="sub">per record, all three sources</span></dd></div>'
            f'<div class="cell good"><dt>Value matched</dt>'
            f'<dd>{_pct(value["matched_paise"], value["total_paise"])}'
            f'<span class="sub">{_esc(_rupees(value["matched_paise"]))}</span></dd></div>'
            f'<div class="cell bad"><dt>Value unmatched</dt>'
            f'<dd>{_pct(value["exceptions_paise"], value["total_paise"])}'
            f'<span class="sub">{_esc(_rupees(value["exceptions_paise"]))}</span></dd></div>'
            "</dl>",
            "One sale is counted up to three times — as a ledger row, a gateway payment, and "
            "inside a bank credit — so this total exceeds the money that moved. The record rate "
            "and the value rate are computed over the same population so that they can be "
            "compared at all.",
        )

    return (
        _section(
            "The result",
            "How much of this run FinCtl settled on its own, and how much it got wrong — "
            "measured against known answers it never reads while matching.",
            f'<dl class="strip">{_strip(run)}</dl>',
        )
        + value_strip
        + _section(
            "How the work was done",
            "Four methods, tried in order, each one handed only what the methods before it "
            "could not settle.",
            f'<div class="card"><div class="cascade">{_cascade(run)}</div></div>',
            "Each bar is how many records arrived at that step, so the bars narrow as the work "
            "gets done. A bar the same width as the one above it means that step settled "
            "nothing on this run — which is a fact about the method, not a drawing error.",
        )
        + _section(
            "The underlying numbers",
            "The same run as printed by the command line, for anyone who wants to check the "
            "figures above against their source.",
            '<div class="card">'
            f'<details class="raw"><summary>'
            f"{_esc('Full metrics, exactly as make eval prints them')}</summary>"
            f'<pre>{_esc(run["block"]) or "Run make eval."}</pre></details>'
            f'<details class="raw"><summary>'
            f"{_esc('Each method measured on its own — every row is a real run, not a subtraction')}"
            f'</summary><pre>{_esc(run["ablation"]) or "Run make eval."}</pre></details>'
            "</div>",
            f"Every arm of that table is a real run over the same {n:,} records with later "
            f"methods switched off, not a subtraction from this one.",
        )
    )


def _page_exceptions(run: dict, links: dict[str, str]) -> str:
    absent = run["by_class"].get("absent", 0)
    undetermined = run["by_class"].get("undetermined", 0)
    items = len(run["exception_items"])
    here = links["exceptions"]

    return (
        _section(
            "What could not be matched",
            f"{items} findings covering {run['exceptions']:,} records, worth "
            f"{_rupees(run['at_risk_paise'])}, ordered with the most money first.",
            _by_type(run) + _filters(run, here=here) + _filter_targets(run)
            + f'<div class="card flush"><div class="queue">{_queue(run)}</div></div>',
            f"Click a row to open it. Refusing to answer is the right outcome where the records "
            f"do not settle the question: {absent} of these have no counterpart in the data at "
            f"all, and {undetermined} have one that cannot be told apart from another. Each open "
            f"row shows the records involved, the alternatives that were weighed, and the audit "
            f"entries written at the time.",
        )
    )


def _page_about(run: dict, links: dict[str, str]) -> str:
    llm = run["llm"]
    per_layer = {int(k): v for k, v in run["per_layer"].items()}
    layer_rows = "".join(
        f"<tr><td>{_esc(_LAYERS[layer][0])}<br>"
        f'<span class="keys">{_esc(_LAYERS[layer][1])}</span></td>'
        f'<td class="num">{per_layer.get(layer, 0):,}</td></tr>'
        for layer in sorted(_LAYERS)
    )

    return (
        _section(
            "Four methods, tried in order",
            "Each one is handed only the records the methods before it could not settle, so "
            "the cheapest and most certain method runs first.",
            '<div class="card"><table class="by-type"><thead><tr><th>Method</th>'
            '<th class="num">Records settled</th></tr></thead>'
            f"<tbody>{layer_rows}</tbody></table></div>",
            "The fourth method is the only one that involves a language model, and it is the "
            "only one that can be wrong in a way the others cannot — which is why nothing it "
            "proposes is trusted.",
        )
        + _section(
            "Nothing is matched until the arithmetic agrees",
            "Every method proposes; one module disposes. It is the single decision this design "
            "rests on.",
            '<div class="card intro">'
            "<p>Each of the four methods produces a <em>proposal</em>: these records belong "
            "together. A proposal is not a match. One module recomputes the settlement total "
            "from the records themselves — never from whatever the proposing method calculated "
            "— and approves only if it agrees with the bank credit <strong>to the paisa</strong>. "
            "There is no tolerance setting, because a tolerance is the mechanism by which wrong "
            "matches enter a ledger while the headline improves.</p>"
            "<p>Two things follow, and both are structural rather than promised. A made-up "
            "match cannot get in, because it fails arithmetic the model does not perform. And "
            "bank narration is untrusted third-party text that reaches the model, so text "
            "shaped like an instruction is in scope — the worst it can achieve is a proposal "
            "that fails the same check. Every reference the model extracts is additionally "
            "checked against the real settlement references before use, so an invented one is "
            "inert.</p>"
            "<p>Refusing is a feature, not a shortfall. A system that confidently matches an "
            "ambiguous pair has excellent coverage and terrible precision. Where the records "
            "genuinely do not settle the question, FinCtl declines and records what it weighed, "
            "so a person can finish the decision it could not.</p>"
            "</div>",
        )
        + _section(
            "What broke while building it",
            "The dominant failure here was not a bug in the matching logic. It was a test that "
            "passed while checking the wrong thing — five separate times.",
            '<div class="card intro">'
            "<p>A check is written, it passes, and the passing is taken as evidence. But the "
            "check tests a stand-in for the property, the stand-in and the property come apart "
            "later, and nothing fails. Confidence accrues that was never earned, in exactly the "
            "area the check was supposed to protect. An absent check leaves a known gap; a wrong "
            "check closes it on paper.</p>"
            "<table class=\"by-type\"><thead><tr><th>The property</th>"
            "<th>What was actually checked</th></tr></thead><tbody>"
            "<tr><td>the bounded search is hard</td><td>how many candidates it had</td></tr>"
            "<tr><td>no record is lost or double-counted</td><td>a total</td></tr>"
            "<tr><td>the held-out data is used once</td><td>a sentence in three documents</td></tr>"
            "<tr><td>the ambiguous case is refused</td><td>a ground-truth label</td></tr>"
            "<tr><td>the deployed build reports its version</td>"
            "<td>a local build configured to pass</td></tr>"
            "</tbody></table>"
            "<p>The habit that came out of it: for every check, ask not &ldquo;does this "
            "pass?&rdquo; but &ldquo;could this pass while the property is false?&rdquo; — and "
            "then break the thing it guards, to see it fail.</p>"
            "</div>",
        )
        + _section(
            "What these numbers do not cover",
            "Stated plainly, because a result reported to one decimal place should be equally "
            "precise about what it does not measure.",
            '<div class="card intro">'
            "<p><strong>The data is synthetic, and it satisfies its author's own model of the "
            "domain.</strong> This is the central limit and it bounds every figure in this "
            "report. The generator and the matcher were written by the same person from the "
            "same reading of the gateway's documentation, so a misreading shared by both is "
            "invisible here. These numbers measure whether the engine solves the problem as "
            "specified — not whether the specification matches production.</p>"
            f"<p><strong>The model's contribution is partly stubbed.</strong> This run reports "
            f"provider <code>{_esc(llm['provider'])}</code> in mode "
            f"<code>{_esc(llm['mode'])}</code>. Two narration shapes were served by a real "
            f"model before a free-tier daily quota was exhausted; the rest come from an offline "
            f"stub, which is a heuristic test double and not a model. Runs say so on the same "
            f"line as their numbers.</p>"
            "<p><strong>Not measured at all:</strong> behaviour on real gateway data, on volumes "
            "beyond this dataset, on settlement cycles that straddle a month boundary, or under "
            "concurrent writes.</p>"
            "</div>",
        )
    )


# --- the ingestion adapter page (D-0027) --------------------------------------------------

_LIMITS = (
    '<ul class="limits">'
    "<li><strong>Test mode only.</strong> Supply test-mode credentials. FinCtl issues no other "
    "kind of request, but the account you point it at is your responsibility.</li>"
    "<li><strong>Read-only, always.</strong> The adapter can only issue <code>GET</code>. There "
    "is no code path in it that creates, updates or deletes anything, so there is nothing to "
    "switch off.</li>"
    "<li><strong>One settlement at a time.</strong> It reads a single settlement's rows for "
    "inspection. It is not a bulk export and not a sync.</li>"
    "<li><strong>The accuracy figures elsewhere in this report do not come from here.</strong> "
    "They are measured on synthetic data that has known answers to be scored against. A real "
    "settlement has none, so its records can be shown but can never enter a rate.</li>"
    "<li><strong>Nothing is stored.</strong> Keys are held in the running process only. They are "
    "never written to disk, never committed, never logged, and never recorded in the audit "
    "ledger.</li>"
    "</ul>"
)

_WHAT_IT_WOULD_DO = (
    "<p>Given a test-mode key pair, this page reads <strong>one</strong> settlement "
    "reconciliation report from the gateway and shows the canonical records FinCtl's ingestion "
    "adapter produces from it — the same record shape the four matching methods consume.</p>"
    "<p>The point is not to improve any number on this site. It is to answer one open question: "
    "the gateway documents the transaction <code>fee</code> as <em>including</em> GST at the "
    "Payment entity, while the dashboard's settlement break-up lists tax and fee as separate "
    "deductions. The two readings differ by exactly the GST — small enough to look like a "
    "rounding bug, large enough to fail every balance check. FinCtl resolves it in one audited "
    "place, and a single real report would settle which reading is right.</p>"
)


def _key_form(action: str, *, key_id: str = "", settlement_id: str = "") -> str:
    """The credential form. Only rendered where there is a server to receive it.

    `POST`, never `GET`: a query string carrying a secret ends up in browser history, in server
    access logs and in any proxy in between. `autocomplete="off"` on the secret so a shared
    browser does not offer it back, and `type="password"` so it is not shoulder-readable.
    """
    return (
        f'<form class="keys" method="post" action="{_esc(action)}">'
        '<p><label for="key_id">Test-mode key id</label>'
        f'<input id="key_id" name="key_id" type="text" inputmode="text" spellcheck="false" '
        f'autocomplete="off" placeholder="rzp_test_…" value="{_esc(key_id)}" required></p>'
        '<p><label for="key_secret">Test-mode key secret</label>'
        '<input id="key_secret" name="key_secret" type="password" autocomplete="off" '
        'placeholder="never stored, never logged" required></p>'
        '<p><label for="settlement_id">Settlement id</label>'
        f'<input id="settlement_id" name="settlement_id" type="text" spellcheck="false" '
        f'autocomplete="off" placeholder="setl_…" value="{_esc(settlement_id)}" required></p>'
        '<p class="two"><label for="year">Year</label>'
        '<input id="year" name="year" type="number" min="2000" max="2100" placeholder="2026" '
        'required>'
        '<label for="month">Month</label>'
        '<input id="month" name="month" type="number" min="1" max="12" placeholder="3" '
        "required></p>"
        '<p><input class="submit" type="submit" value="Read this settlement"></p>'
        '<p class="caption">Sent once, over the wire, to fetch one report. Held in the running '
        "process for that request and nowhere else.</p>"
        "</form>"
    )


def _adapted_records(ingest: dict) -> str:
    """The canonical records the adapter produced, and everything it could not read."""
    rows = ingest.get("rows") or []
    skipped = ingest.get("skipped") or []

    if not rows and not skipped:
        return (
            '<div class="empty"><strong>That settlement has no rows in the report.</strong>'
            "The credentials worked and the report came back, but no transaction in it carries "
            "this settlement id. Check the id, the year and the month.</div>"
        )

    body = "".join(
        f'<tr><td class="rid">{_esc(row["row_id"])}</td>'
        f'<td>{_esc(row["type"])}</td>'
        f'<td class="num">{_esc(_rupees(row["credit_paise"]))}</td>'
        f'<td class="num">{_esc(_rupees(row["debit_paise"]))}</td>'
        f'<td class="num">{_esc(_rupees(row["fee_base_paise"]))}</td>'
        f'<td class="num">{_esc(_rupees(row["gst_paise"]))}</td>'
        f'<td class="num">{_esc(_rupees(row["net_paise"]))}</td></tr>'
        for row in rows
    )
    table = (
        '<div class="ev-wrap"><table class="ev"><thead><tr><th>Record</th><th>Type</th>'
        '<th class="num">Credit</th><th class="num">Debit</th>'
        '<th class="num">Fee, ex GST</th><th class="num">GST</th>'
        '<th class="num">Net</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
        if rows
        else ""
    )

    skipped_block = ""
    if skipped:
        items = "".join(
            f"<li><code>{_esc(row_id)}</code> — {_esc(reason)}</li>" for row_id, reason in skipped
        )
        skipped_block = (
            f'<h3>Not adapted <span class="flag">{len(skipped)}</span></h3>'
            '<p class="note">Reported rather than dropped: a count that silently excludes what '
            "it could not read is the same failure as a rate computed over an undisclosed "
            f"subset.</p><ul class=\"limits\">{items}</ul>"
        )

    total = ingest.get("net_total_paise")
    footer = ""
    if rows and isinstance(total, int):
        footer = (
            f'<p class="note">These {len(rows)} rows net to '
            f"<strong>{_esc(_rupees(total))}</strong>. That is what FinCtl would expect the bank "
            "credit for this settlement to be, under the GST-inclusive reading of the fee. If "
            "the real credit differs by roughly the GST, the other reading is the right one — "
            "which is the question this page exists to answer.</p>"
        )

    return (
        f'<h3>Canonical records <span class="ok">{len(rows)}</span></h3>'
        + table
        + footer
        + skipped_block
    )


def _page_settings(run: dict, links: dict[str, str], ingest: dict | None = None) -> str:
    """Read one real settlement through the adapter. Off by default, read-only always.

    Five states, and each one has to be honest about what it is:

    * `demo`   — `DEMO_MODE=1`, the deployed default. Disabled outright, and says why.
    * `static` — the exported files. There is no server to receive a credential, so no form is
      drawn. A form posting nowhere is a dead control, which is worse than no control.
    * `ready`  — a server, not in demo mode. The form is live.
    * `result` — a report was read and adapted.
    * `error`  — the attempt failed, with a message that never contains the credential.
    """
    ingest = ingest or {"mode": "static"}
    mode = ingest.get("mode", "static")
    parts: list[str] = []

    if mode == "demo":
        parts.append(
            '<div class="card intro"><p class="warn"><strong>Disabled in this deployment.</strong> '
            "This instance runs with <code>DEMO_MODE=1</code>, which serves a pre-computed run "
            "from committed fixtures and holds no credentials of any kind. Reading live data is "
            "switched off here deliberately: a public demo should not be a box that invites "
            "strangers to type a key into it.</p>"
            "<p>To use it, run FinCtl locally with <code>make serve</code> and open "
            "<code>/settings</code> there.</p></div>"
        )
        parts.append(f'<div class="card intro">{_WHAT_IT_WOULD_DO}</div>')
    elif mode == "static":
        parts.append(
            '<div class="card intro"><p class="warn"><strong>Not available on this page.</strong> '
            "You are reading the static export — five HTML files with no server behind them. "
            "There is nothing here to receive a credential, so no form is drawn rather than one "
            "that would post into the void.</p>"
            "<p>Run FinCtl locally with <code>make serve</code> and open <code>/settings</code> "
            "to use it.</p></div>"
        )
        parts.append(f'<div class="card intro">{_WHAT_IT_WOULD_DO}</div>')
    else:
        parts.append(f'<div class="card intro">{_WHAT_IT_WOULD_DO}</div>')

        if ingest.get("error"):
            parts.append(
                f'<div class="card"><p class="warn"><strong>That did not work.</strong> '
                f'{_esc(ingest["error"])}</p></div>'
            )
        if ingest.get("warning"):
            parts.append(f'<div class="card"><p class="warn">{_esc(ingest["warning"])}</p></div>')

        parts.append(
            '<div class="card">'
            + _key_form(
                links["settings"],
                key_id=ingest.get("key_id", ""),
                settlement_id=ingest.get("settlement_id", ""),
            )
            + "</div>"
        )

        if mode == "result":
            parts.append(f'<div class="card">{_adapted_records(ingest)}</div>')
            others = ingest.get("settlement_ids") or []
            if others:
                shown = ", ".join(_esc(s) for s in others[:12])
                parts.append(
                    f'<p class="caption">Settlements present in that report: {shown}'
                    f'{" …" if len(others) > 12 else ""}.</p>'
                )

    return _section("Reading a real settlement", PAGES["settings"][4], "".join(parts)) + _section(
        "What this does not do",
        "The limits are part of the feature, so they are stated here rather than in a footnote.",
        f'<div class="card intro">{_LIMITS}</div>',
    )


# --- assembly -----------------------------------------------------------------------------


PAGE_BODIES = {
    "overview": _page_overview,
    "run": _page_run,
    "exceptions": _page_exceptions,
    "about": _page_about,
}


def _provenance_bar(run: dict) -> str:
    prov = run["provenance"]
    llm = run["llm"]
    return " ".join(
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


def build_page(
    run: dict,
    *,
    page: str = "overview",
    links: dict[str, str] | None = None,
    ingest: dict | None = None,
) -> str:
    """Render one page. Raises `FileNotFoundError` if `web/` is missing.

    `links` selects how a link is spelled — routes for the live app, relative filenames for the
    static export (D-0026). Everything else about the two is identical by construction.

    `ingest` carries the settings page's per-request state and is **deliberately not part of
    `run`**: `run` is inlined verbatim into the JSON block on every page, so anything placed in it
    is published. Nothing derived from a credential may go there (item 5).
    """
    if page not in PAGES:
        raise ValueError(f"unknown page {page!r}; expected one of {sorted(PAGES)}")
    links = links if links is not None else SERVER_LINKS

    template = (WEB_DIR / "page.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    _, _, _, title, says = PAGES[page]

    body = (
        PAGE_BODIES[page](run, links)
        if page in PAGE_BODIES
        else _page_settings(run, links, ingest)
    )

    n = run["records"]
    matched = run["auto_matched"]

    replacements = {
        "%%TITLE%%": _esc(f"{title} — FinCtl"),
        "%%DESCRIPTION%%": _esc(says),
        "%%CSS%%": css,
        "%%H1%%": _esc(title),
        # The page's own sentence, not one generic line repeated five times. A reader who lands
        # here from a link needs to know what *this* page is, and a tagline that is identical
        # everywhere tells them nothing they could not already see in the nav.
        "%%TAGLINE%%": _esc(says),
        "%%NAV%%": _nav(links, page),
        "%%PROVENANCE%%": _provenance_bar(run),
        "%%BANNER%%": _banner(run),
        "%%MAIN%%": body + _onward(links, page),
        "%%FOOTER%%": (
            f"{matched:,} of {n:,} records matched in {run['wall_clock_ms']:,} ms. "
            f"Amounts are held as whole paise throughout and turned into rupees only here, at "
            f"the point of display. Nothing on this page runs, and it requests nothing from the "
            f"network."
        ),
        # Every page carries the WHOLE run, not the slice it happens to render. That costs some
        # bytes and buys a property worth more than them: two pages of this export cannot
        # disagree about a figure, because there is only one copy of it and they all inline the
        # same one. `</` is the only sequence that can terminate a script element early, so
        # escaping it is what keeps inlined data from breaking out of the block.
        "%%DATA%%": json.dumps(run, indent=2, sort_keys=True, default=str).replace("</", "<\\/"),
    }

    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    leftover = [token for token in replacements if token in rendered]
    if leftover:  # pragma: no cover - guards against a token renamed in only one file
        raise RuntimeError(f"template tokens not substituted: {leftover}")
    return rendered


def build_html(
    run: dict,
    *,
    page: str = "overview",
    links: dict[str, str] | None = None,
    ingest: dict | None = None,
) -> str:
    """Backwards-compatible alias. `api/main.py` and the tests both enter through here."""
    return build_page(run, page=page, links=links, ingest=ingest)


def write_static(run: dict, out_dir: Path) -> list[Path]:
    """Write every page as a real file with relative links between them.

    Real files rather than one page plus fragments, so GitHub Pages serves the same navigation
    with no server and no rewrite rules, and so the export works from `file://` too.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for key in NAV_ORDER:
        path = out_dir / PAGES[key][0]
        path.write_text(build_page(run, page=key, links=STATIC_LINKS), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render the static run report")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--dataset", default="dev_seed_11")
    args = parser.parse_args(argv)

    from api.main import compute_run

    run = compute_run(args.dataset)
    # Resolve before use: `--out-dir docs` is relative, and reporting it against the repo root
    # without resolving raised rather than printing.
    out_dir = args.out_dir if args.out_dir.is_absolute() else (Path.cwd() / args.out_dir).resolve()
    written = write_static(run, out_dir)

    total = 0
    for path in written:
        size = path.stat().st_size
        total += size
        try:
            shown = path.relative_to(REPO_ROOT)
        except ValueError:
            shown = path
        print(f"wrote {shown} ({size:,} bytes)")
    print(f"{len(written)} pages, {total:,} bytes, inlined, no script, no fetch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
