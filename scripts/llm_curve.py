"""Measure the LLM call curve across repeated runs. `make llm-curve`.

Two curves, because two different caches make calls fall and conflating them would overstate
what the regex promotion achieves:

**Curve A — the regex cache, isolated.** The fixture cache is cleared before every run while the
rules cache persists. So nothing is replayed: every falling call is a narration shape that a
*promoted regex* now handles deterministically. This is the curve that says the model is writing
rules rather than participating in every run.

**Curve B — the fixture cache.** Nothing is cleared. Calls fall to zero because responses are
replayed by prompt hash. This is what makes `DEMO_MODE=1` free and replay byte-identical, and it
would fall to zero even if no regex were ever promoted — which is exactly why reporting only
this curve would be misleading.

Run with `--runs N`. Prints a table; nothing here is typed by hand into a document.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures" / "llm"
RULES = REPO_ROOT / "fixtures" / "rules_cache.json"


def _clear_fixtures() -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True, exist_ok=True)


def _clear_rules() -> None:
    RULES.unlink(missing_ok=True)


def _run(dataset: str):
    # Imported per call so the rules cache is re-read from disk each time rather than held in a
    # module-level cache, which would make the curve an artefact of process lifetime.
    from eval import harness

    return harness.evaluate(dataset, max_layer=4)


def curve(dataset: str, runs: int, *, keep_fixtures: bool) -> list[tuple[int, int, int, int, int]]:
    rows = []
    for index in range(1, runs + 1):
        if not keep_fixtures:
            _clear_fixtures()
        metrics = _run(dataset)
        rows.append(
            (
                index,
                metrics.llm_calls,
                metrics.llm_cache_hits,
                metrics.rules_promoted,
                metrics.n,
                metrics.llm_calls_by_kind.get("narration_parse", 0),
                metrics.llm_calls_by_kind.get("exception_explanation", 0),
            )
        )
    return rows


def _render(title: str, note: str, rows: list[tuple[int, int, int, int, int]]) -> str:
    lines = [
        title,
        f"  {note}",
        "",
        "  run   calls   per 100   PARSE   explain   cache hits   promoted rules",
    ]
    for index, calls, hits, promoted, n, parse, explain in rows:
        lines.append(
            f"  {index:>3}   {calls:>5}   {100 * calls / n:>7.2f}   {parse:>5}   {explain:>7}   "
            f"{hits:>10}   {promoted:>14}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM call curve across repeated runs")
    parser.add_argument("--dataset", default="dev_seed_11")
    parser.add_argument("--runs", type=int, default=4)
    args = parser.parse_args(argv)

    if os.environ.get("DEMO_MODE") == "1":
        parser.error("DEMO_MODE=1 replays from fixtures, so no curve can be measured")

    _clear_fixtures()
    _clear_rules()
    a = curve(args.dataset, args.runs, keep_fixtures=False)

    _clear_fixtures()
    _clear_rules()
    b = curve(args.dataset, args.runs, keep_fixtures=True)

    print(
        _render(
            "Curve A - the regex cache, isolated",
            "fixtures cleared before every run, rules cache kept. Nothing is replayed, so a "
            "falling call count is a narration shape a promoted regex now handles.",
            a,
        )
    )
    print()
    print(
        _render(
            "Curve B - the fixture cache",
            "nothing cleared. Calls fall because responses replay by prompt hash. This would "
            "fall to zero even with no regex ever promoted, which is why A is reported too.",
            b,
        )
    )
    print()
    parse_first, parse_last = a[0][5], a[-1][5]
    print(
        f"PARSE calls: {parse_first} -> {parse_last} over {len(a)} runs, with "
        f"{a[-1][3]} regexes promoted. Nothing was replayed, so that is the regex cache."
    )
    print(
        f"EXPLAIN calls hold at {a[-1][6]}: one per distinct exception type, not a narration "
        "shape, so no regex can retire them. They fall only in curve B, via the fixture cache."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
