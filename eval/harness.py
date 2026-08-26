"""Evaluation harness — lands in Phase 2, before most of the matcher exists.

The measurement is the product: this module is built ahead of the layers it scores,
so every later improvement is measured against a recorded baseline rather than
against a remembered one.

Prints the metrics block defined in ``.claude/skills/eval-protocol/SKILL.md``. That
stdout is pasted verbatim into ``docs/METRICS.md`` — no number is ever retyped.
"""

from __future__ import annotations

PHASE = 2

if __name__ == "__main__":
    raise SystemExit(
        "FinCtl: eval.harness lands in Phase 2. Current phase: see docs/PROGRESS.md."
    )
