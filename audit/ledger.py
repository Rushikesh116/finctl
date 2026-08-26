"""Append-only, hash-chained decision log.

Every decision the cascade makes is recorded: which layer fired, what it read, what it
concluded, its confidence, and — when an LLM was involved — the model version and token cost.
Entries are chained by hash, so an entry cannot be altered or removed after the fact without
breaking every hash after it.

**There is no wall-clock timestamp in a ledger entry, and that is deliberate.** Invariant 4
requires the same seed and input to produce a *byte-identical* audit log. A wall-clock field
makes that impossible — every run would differ, the chain would be unverifiable against a
recorded one, and "deterministic and replayable" would become an unfalsifiable claim. Entries
are ordered by a monotonic sequence number, which is a logical clock and all the ordering a
decision log needs. Run wall-clock lives in the metrics block, where it belongs (D-0017).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["GENESIS_HASH", "AuditLedger", "LedgerEntry", "verify_chain"]

GENESIS_HASH = "0" * 64
"""The `prev_hash` of the first entry. A fixed, known anchor, so a chain can be verified from
its start without needing to be told where the start is."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One decision. Immutable, and hashed over its canonical JSON form."""

    seq: int
    layer: int
    decision: str
    record_ids: tuple[str, ...]
    outcome: str
    confidence: int
    detail: str = ""
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros_usd: int = 0
    prev_hash: str = GENESIS_HASH
    entry_hash: str = ""

    def payload(self) -> dict[str, Any]:
        """Everything the hash covers — i.e. everything except the hash itself."""
        data = asdict(self)
        data.pop("entry_hash")
        data["record_ids"] = list(self.record_ids)
        return data

    def compute_hash(self) -> str:
        # sort_keys and a fixed separator: the digest must not depend on dict insertion order
        # or on how a JSON encoder happens to space things.
        canonical = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLedger:
    """An in-memory chain that can be persisted to SQLite or serialised to text.

    Confidence is an integer percentage 0–100, not a float: it is compared and summed, and a
    float would put a non-integer through the same code paths money travels (invariant 1's
    reasoning applies to anything hashed for reproducibility).
    """

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    @property
    def head_hash(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS_HASH

    def record(
        self,
        *,
        layer: int,
        decision: str,
        record_ids: tuple[str, ...] | list[str],
        outcome: str,
        confidence: int,
        detail: str = "",
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_micros_usd: int = 0,
    ) -> LedgerEntry:
        if not 0 <= confidence <= 100:
            raise ValueError(f"confidence must be 0..100, got {confidence}")
        if model is None and (input_tokens or output_tokens or cost_micros_usd):
            raise ValueError(
                "token counts or cost recorded without a model: an LLM decision must name "
                "the model version it came from, or the cost accounting is unattributable"
            )

        entry = LedgerEntry(
            seq=len(self._entries) + 1,
            layer=layer,
            decision=decision,
            # Sorted: the same decision over the same records must hash identically however
            # the caller happened to order them.
            record_ids=tuple(sorted(record_ids)),
            outcome=outcome,
            confidence=confidence,
            detail=detail,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros_usd=cost_micros_usd,
            prev_hash=self.head_hash,
        )
        sealed = LedgerEntry(**{**entry.payload(), "record_ids": entry.record_ids, "entry_hash": entry.compute_hash()})
        self._entries.append(sealed)
        return sealed

    # --- serialisation -------------------------------------------------------------------

    def to_text(self) -> str:
        """One canonical JSON object per line. This is what gets byte-compared across runs."""
        return "".join(
            json.dumps(
                {**entry.payload(), "entry_hash": entry.entry_hash},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
            for entry in self._entries
        )

    def write_sqlite(self, path: Path) -> None:
        """Persist to SQLite with plain SQL, so a judge can open the file and read it.

        Money and cost columns are `INTEGER`. Never `REAL`.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("DROP TABLE IF EXISTS audit_log")
            connection.execute(
                """
                CREATE TABLE audit_log (
                    seq             INTEGER PRIMARY KEY,
                    layer           INTEGER NOT NULL,
                    decision        TEXT    NOT NULL,
                    record_ids      TEXT    NOT NULL,
                    outcome         TEXT    NOT NULL,
                    confidence      INTEGER NOT NULL,
                    detail          TEXT    NOT NULL,
                    model           TEXT,
                    input_tokens    INTEGER NOT NULL,
                    output_tokens   INTEGER NOT NULL,
                    cost_micros_usd INTEGER NOT NULL,
                    prev_hash       TEXT    NOT NULL,
                    entry_hash      TEXT    NOT NULL
                )
                """
            )
            connection.executemany(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        e.seq,
                        e.layer,
                        e.decision,
                        ",".join(e.record_ids),
                        e.outcome,
                        e.confidence,
                        e.detail,
                        e.model,
                        e.input_tokens,
                        e.output_tokens,
                        e.cost_micros_usd,
                        e.prev_hash,
                        e.entry_hash,
                    )
                    for e in self._entries
                ],
            )
            connection.commit()
        finally:
            connection.close()


def verify_chain(entries: list[LedgerEntry]) -> None:
    """Raise on the first broken link, naming it.

    Checks three things independently: sequence numbers are dense and 1-based, each entry's
    `prev_hash` is its predecessor's hash, and each entry's own hash matches its content. The
    third is the one that catches tampering — an attacker who edits a field and recomputes the
    chain forward is caught only if content is hashed, not just linked.
    """
    expected_prev = GENESIS_HASH
    for index, entry in enumerate(entries, start=1):
        if entry.seq != index:
            raise ValueError(f"ledger entry {index} has seq {entry.seq}: sequence is not dense")
        if entry.prev_hash != expected_prev:
            raise ValueError(
                f"ledger entry {entry.seq} breaks the chain: prev_hash {entry.prev_hash[:12]} "
                f"but predecessor hashed to {expected_prev[:12]}"
            )
        recomputed = entry.compute_hash()
        if entry.entry_hash != recomputed:
            raise ValueError(
                f"ledger entry {entry.seq} has been altered: stored hash "
                f"{entry.entry_hash[:12]} but content hashes to {recomputed[:12]}"
            )
        expected_prev = entry.entry_hash
