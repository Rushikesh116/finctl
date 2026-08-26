"""Synthetic dataset generator — lands in Phase 1.

Emits three sources (merchant ledger, gateway records, bank statement) plus a
*separate* ground-truth labels file. Every record either names its true partner or
is explicitly labelled unmatchable with a reason code.

Direction of dependency matters: this module imports record schemas *from* ``core``.
``core`` must never import this module, or the matcher could read labels. That rule
is enforced by ``tests/test_invariants.py::test_core_never_imports_ground_truth``.
"""

from __future__ import annotations

PHASE = 1

if __name__ == "__main__":
    raise SystemExit(
        "FinCtl: data.generator lands in Phase 1. Current phase: see docs/PROGRESS.md."
    )
