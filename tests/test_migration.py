"""Upgrade path from the first prototype's database layout."""

from __future__ import annotations

import sqlite3

from leadsbot import logic, storage

LEGACY_SCHEMA = """
CREATE TABLE conversations (
    account_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    name TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'collecting',
    PRIMARY KEY (account_id, sender_id)
);
CREATE TABLE processed_messages (
    message_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
"""


def test_legacy_database_is_migrated(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.execute(
        "INSERT INTO conversations VALUES ('123','u1','2026-01-01T00:00:00+00:00',"
        "'Elvin','+994501234567','done')"
    )
    con.execute("INSERT INTO processed_messages VALUES ('old-mid','2026-01-01T00:00:00+00:00')")
    con.commit()
    con.close()

    storage.init_db(path)

    con = storage.connect(path)
    try:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(conversations)")}
        processed = {row["name"] for row in con.execute("PRAGMA table_info(processed_messages)")}
    finally:
        con.close()
    assert {"updated_at", "hints"} <= columns
    assert {"account_id", "message_id"} <= processed

    lead = storage.leads(path)[0]
    assert (lead["name"], lead["status"]) == ("Elvin", "done")
    assert lead["updated_at"] == "2026-01-01T00:00:00+00:00"

    # Already-finished conversations stay silent, and new messages are still tracked.
    plan = storage.intake(path, "123", "u1", "new-mid", "Salam")
    assert plan is not None and plan.reply is None
    assert storage.intake(path, "123", "u1", "new-mid", "Salam") is None


def test_schema_is_idempotent(tmp_path):
    path = str(tmp_path / "fresh.sqlite3")
    storage.init_db(path)
    storage.init_db(path)
    assert storage.stats(path)["total"] == 0
    plan = storage.intake(path, "123", "u1", "m1", "Salam")
    assert plan is not None and plan.reply == logic.GREETING