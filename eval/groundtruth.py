"""Ground-truth loading. Lives in `eval/` because nothing in `core/` may read it.

The import direction is the enforcement: `core` cannot reach this module, so the matcher
cannot see the answers however carelessly it is later extended
(`tests/test_invariants.py::test_core_never_imports_ground_truth`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.records import Label, SettlementLabel

__all__ = ["GroundTruth", "load_ground_truth"]


@dataclass(frozen=True, slots=True)
class GroundTruth:
    record_labels: list[Label]
    settlement_labels: list[SettlementLabel]

    def by_row_id(self) -> dict[str, Label]:
        return {label.row_id: label for label in self.record_labels}

    def true_groups(self) -> dict[str, frozenset[str]]:
        """Every record's true group, as a set of record ids.

        Unmatchable records map to the empty set, which makes the set-equality comparison in
        `SPEC.md` §4 / eval-protocol §4 uniform: matching an unmatchable record fails equality
        against the empty set with no special case.
        """
        members: dict[str, list[str]] = {}
        for label in self.record_labels:
            if label.true_group_id:
                members.setdefault(label.true_group_id, []).append(label.row_id)

        frozen = {group: frozenset(ids) for group, ids in members.items()}
        return {
            label.row_id: frozen.get(label.true_group_id or "", frozenset())
            for label in self.record_labels
        }


def load_ground_truth(path: Path) -> GroundTruth:
    payload = json.loads(path.read_text(encoding="utf-8"))

    records = [
        Label(
            row_id=entry["row_id"],
            source=entry["source"],
            pathologies=list(entry["pathologies"]),
            true_group_id=entry.get("true_group_id"),
            unmatchable=bool(entry.get("unmatchable", False)),
            reason_code=entry.get("reason_code"),
        )
        for entry in payload["records"]
    ]
    settlements = [
        SettlementLabel(
            settlement_id=entry["settlement_id"],
            settlement_utr=entry.get("settlement_utr"),
            bank_row_id=entry.get("bank_row_id"),
            mechanism=entry.get("mechanism"),
            true_member_row_ids=list(entry["true_member_row_ids"]),
            delta_paise=entry["delta_paise"],
            pool_row_ids=list(entry.get("pool_row_ids", [])),
            explaining_subsets=[list(s) for s in entry.get("explaining_subsets", [])],
        )
        for entry in payload["settlements"]
    ]
    return GroundTruth(record_labels=records, settlement_labels=settlements)
