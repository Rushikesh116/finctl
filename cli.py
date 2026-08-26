"""FinCtl command line — ``reconcile`` lands in Phase 2.

Drives one reconciliation run over a named dataset and writes the match ledger and
the hash-chained audit log to SQLite. Deliberately thin: all logic lives in ``core``
and ``audit`` so the same code path serves the CLI, the API, and the harness.
"""

from __future__ import annotations

PHASE = 2

if __name__ == "__main__":
    raise SystemExit(
        "FinCtl: `cli reconcile` lands in Phase 2. Current phase: see docs/PROGRESS.md."
    )
