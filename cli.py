"""FinCtl command line.

Deliberately thin. All logic lives in `core`, `audit` and `eval`, so the CLI, the API and the
harness drive exactly the same code path — a reconciliation you can only reproduce through one
entry point is not reproducible.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from core.config import load_dotenv
from data.generator import DATASET_SEEDS
from eval import harness

PHASE = 2


def reconcile(dataset: str, db_path: Path) -> int:
    metrics = harness.evaluate(dataset, db_path=db_path)
    print(harness.render(metrics))
    print()
    print(f"audit ledger written to {db_path} ({metrics.ledger_entries} entries)")
    print(f"  chain head {metrics.ledger_head}")
    if not metrics.provenance.is_trustworthy:
        print(
            "  WARNING: provenance is not trustworthy "
            f"({metrics.provenance.manifest_state}) — this run is not tied to committed data"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="finctl", description="FinCtl reconciliation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("reconcile", help="reconcile a dataset and write SQLite")
    run.add_argument("--dataset", default="dev_seed_11", choices=sorted(DATASET_SEEDS))
    run.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("FINCTL_DB_PATH", "finctl.db")),
        help="SQLite output path (FINCTL_DB_PATH)",
    )

    args = parser.parse_args(argv)
    if args.command == "reconcile":
        return reconcile(args.dataset, args.db)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
