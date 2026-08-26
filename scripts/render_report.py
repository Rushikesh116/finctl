"""Static report renderer — lands in Phase 6.

Renders the same single page the live UI serves into ``docs/index.html`` with the run
data inlined: no server, no fetch, no build step. That file is the zero-infrastructure
fallback on GitHub Pages, so results stay visible even when the live service is asleep.
"""

from __future__ import annotations

PHASE = 6

if __name__ == "__main__":
    raise SystemExit(
        "FinCtl: scripts.render_report lands in Phase 6. Current phase: see docs/PROGRESS.md."
    )
