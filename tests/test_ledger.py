"""Tests for the hash-chained audit ledger.

The two properties worth having are that tampering is detectable and that a replay is
byte-identical. Both are easy to claim and easy to get subtly wrong, so both are tested by
doing the thing rather than by inspecting the code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from audit.ledger import GENESIS_HASH, AuditLedger, verify_chain


def build(count: int = 4) -> AuditLedger:
    ledger = AuditLedger()
    for index in range(1, count + 1):
        ledger.record(
            layer=1,
            decision="approve_group",
            record_ids=[f"gw_{index}", f"bk_{index}"],
            outcome=f"grp_{index}",
            confidence=100,
            detail=f"balanced at zero tolerance ({index})",
        )
    return ledger


def test_a_clean_chain_verifies() -> None:
    verify_chain(build().entries)


def test_the_first_entry_anchors_to_a_known_genesis() -> None:
    """A chain must be verifiable from its start without being told where the start is."""
    assert build(1).entries[0].prev_hash == GENESIS_HASH


def test_each_entry_links_to_its_predecessor() -> None:
    entries = build(3).entries
    for previous, current in zip(entries, entries[1:]):
        assert current.prev_hash == previous.entry_hash


def test_altering_a_field_is_detected() -> None:
    """The content hash, not just the link, is what catches tampering."""
    entries = build(3).entries
    entries[1] = replace(entries[1], outcome="grp_tampered")

    with pytest.raises(ValueError, match="has been altered"):
        verify_chain(entries)


def test_removing_an_entry_is_detected() -> None:
    entries = build(4).entries
    del entries[2]

    with pytest.raises(ValueError, match="sequence is not dense|breaks the chain"):
        verify_chain(entries)


def test_reordering_entries_is_detected() -> None:
    entries = build(4).entries
    entries[1], entries[2] = entries[2], entries[1]

    with pytest.raises(ValueError):
        verify_chain(entries)


def test_two_identical_runs_produce_byte_identical_logs() -> None:
    """Invariant 4. This is why there is no wall-clock field in an entry (D-0017).

    A timestamp would make every run differ, the chain unverifiable against a recorded one,
    and "deterministic and replayable" an unfalsifiable claim.
    """
    assert build(6).to_text() == build(6).to_text()


def test_no_entry_carries_a_wall_clock_field() -> None:
    """Guards the reason above against a well-meaning future addition."""
    payload = build(1).entries[0].payload()
    forbidden = {"timestamp", "at", "created_at", "recorded_at", "wall_clock", "time"}
    assert not forbidden & set(payload), f"a wall-clock field appeared: {payload.keys()}"


def test_record_id_order_does_not_change_the_hash() -> None:
    """The same decision over the same records must hash identically."""
    one, two = AuditLedger(), AuditLedger()
    one.record(layer=1, decision="d", record_ids=["b", "a", "c"], outcome="o", confidence=50)
    two.record(layer=1, decision="d", record_ids=["c", "a", "b"], outcome="o", confidence=50)

    assert one.head_hash == two.head_hash


def test_a_different_decision_produces_a_different_hash() -> None:
    """Sanity in the other direction: the hash must actually depend on the content."""
    one, two = AuditLedger(), AuditLedger()
    one.record(layer=1, decision="approve_group", record_ids=["a"], outcome="o", confidence=100)
    two.record(layer=1, decision="raise_exception", record_ids=["a"], outcome="o", confidence=100)

    assert one.head_hash != two.head_hash


def test_confidence_outside_0_to_100_is_refused() -> None:
    with pytest.raises(ValueError, match="confidence"):
        AuditLedger().record(
            layer=1, decision="d", record_ids=["a"], outcome="o", confidence=101
        )


def test_token_cost_without_a_model_is_refused() -> None:
    """Cost accounting that cannot name its model is unattributable."""
    with pytest.raises(ValueError, match="without a model"):
        AuditLedger().record(
            layer=4,
            decision="adjudicate",
            record_ids=["a"],
            outcome="proposal",
            confidence=80,
            input_tokens=1200,
        )


def test_sqlite_export_is_readable_and_stores_money_as_integers(tmp_path: Path) -> None:
    """A judge should be able to open the file and read it with plain SQL."""
    path = tmp_path / "run.db"
    build(3).write_sqlite(path)

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT seq, layer, outcome, confidence, prev_hash, entry_hash FROM audit_log "
            "ORDER BY seq"
        ).fetchall()
        types = {
            name: declared
            for _, name, declared, *_ in connection.execute("PRAGMA table_info(audit_log)")
        }
    finally:
        connection.close()

    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[0][4] == GENESIS_HASH
    for column in ("confidence", "input_tokens", "output_tokens", "cost_micros_usd"):
        assert types[column] == "INTEGER", f"{column} is {types[column]}, must be INTEGER"
