"""SQLite persistence: conversations, processed messages, outbox.

The outbox is the durability boundary: a reply is written in the same
transaction as the conversation state it belongs to, so a crash between
"decided to answer" and "asked Instagram to send" can never lose a message.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .logic import STATUS_NEW, Plan, plan_reply

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    account_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    hints INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, sender_id)
);
CREATE TABLE IF NOT EXISTS processed_messages (
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (account_id, message_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    locked_until TEXT,
    sent_at TEXT,
    failed_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox (sent_at, failed_at, next_attempt_at);
"""

# Indexes are created after column migrations so old databases upgrade cleanly.
INDEXES = """
CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox (sent_at, failed_at, next_attempt_at);
CREATE INDEX IF NOT EXISTS conversations_status_idx ON conversations (status, updated_at);
"""

# Columns added after the first prototype; ALTER TABLE keeps old databases usable.
MIGRATIONS = {
    "conversations": {
        "updated_at": "TEXT",
        "hints": "INTEGER NOT NULL DEFAULT 0",
    },
    "outbox": {},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(db_path: str) -> None:
    con = connect(db_path)
    try:
        con.executescript(SCHEMA)
        # First prototype keyed processed_messages by message_id alone; a message
        # only exists within one account, so the key is now (account_id, message_id).
        processed_columns = {row["name"] for row in con.execute("PRAGMA table_info(processed_messages)")}
        if "account_id" not in processed_columns:
            con.execute("DROP TABLE processed_messages")
            con.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Old rows predate updated_at; backfill so ordering stays sane.
        con.execute("UPDATE conversations SET updated_at=started_at WHERE updated_at IS NULL")
        con.executescript(INDEXES)
        con.commit()
    finally:
        con.close()


def intake(
    db_path: str,
    account_id: str,
    sender_id: str,
    message_id: str,
    text: str,
    max_hints: int = 3,
) -> Plan | None:
    """Record an incoming DM and enqueue the reply. None means "already handled"."""
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        duplicate = con.execute(
            "SELECT 1 FROM processed_messages WHERE account_id=? AND message_id=?",
            (account_id, message_id),
        ).fetchone()
        if duplicate:
            con.rollback()
            return None
        stamp = now()
        con.execute(
            "INSERT INTO processed_messages(account_id, message_id, received_at) VALUES (?,?,?)",
            (account_id, message_id, stamp),
        )
        row = con.execute(
            "SELECT name, phone, status, hints FROM conversations"
            " WHERE account_id=? AND sender_id=?",
            (account_id, sender_id),
        ).fetchone()
        if row is None:
            plan = plan_reply(None, None, STATUS_NEW, 0, text, max_hints)
            con.execute(
                "INSERT INTO conversations"
                " (account_id, sender_id, started_at, updated_at, name, phone, status, hints)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (account_id, sender_id, stamp, stamp, plan.name, plan.phone, plan.status, plan.hints),
            )
        else:
            plan = plan_reply(row["name"], row["phone"], row["status"], row["hints"], text, max_hints)
            con.execute(
                "UPDATE conversations SET name=?, phone=?, status=?, hints=?, updated_at=?"
                " WHERE account_id=? AND sender_id=?",
                (plan.name, plan.phone, plan.status, plan.hints, stamp, account_id, sender_id),
            )
        if plan.reply:
            enqueue(con, account_id, sender_id, plan.reply, stamp)
        con.commit()
        return plan
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def enqueue(
    con: sqlite3.Connection,
    account_id: str,
    sender_id: str,
    body: str,
    created_at: str | None = None,
) -> int:
    created_at = created_at or now()
    cursor = con.execute(
        "INSERT INTO outbox (account_id, sender_id, body, created_at, next_attempt_at)"
        " VALUES (?,?,?,?,?)",
        (account_id, sender_id, body, created_at, created_at),
    )
    return int(cursor.lastrowid)


def claim_due(db_path: str, limit: int = 20, lease_seconds: int = 120) -> list[dict[str, Any]]:
    """Reserve due outbox rows so two workers (or a reload) cannot double-send."""
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        stamp = now()
        rows = con.execute(
            "SELECT id, account_id, sender_id, body, attempts FROM outbox"
            " WHERE sent_at IS NULL AND failed_at IS NULL"
            "   AND next_attempt_at <= ?"
            "   AND (locked_until IS NULL OR locked_until <= ?)"
            " ORDER BY id LIMIT ?",
            (stamp, stamp, limit),
        ).fetchall()
        lease = later(lease_seconds)
        for row in rows:
            con.execute("UPDATE outbox SET locked_until=? WHERE id=?", (lease, row["id"]))
        con.commit()
        return [dict(row) for row in rows]
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_sent(db_path: str, outbox_id: int) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE outbox SET sent_at=?, locked_until=NULL, last_error=NULL WHERE id=?",
            (now(), outbox_id),
        )
        con.commit()
    finally:
        con.close()


def mark_retry(db_path: str, outbox_id: int, error: str, attempts: int, delay: float) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE outbox SET attempts=?, locked_until=NULL, next_attempt_at=?, last_error=?"
            " WHERE id=?",
            (attempts, later(delay), error[:500], outbox_id),
        )
        con.commit()
    finally:
        con.close()


def mark_failed(db_path: str, outbox_id: int, error: str, attempts: int = 0) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE outbox SET failed_at=?, locked_until=NULL, attempts=?, last_error=? WHERE id=?",
            (now(), attempts, error[:500], outbox_id),
        )
        con.commit()
    finally:
        con.close()


def leads(db_path: str, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        sql = "SELECT * FROM conversations"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def stats(db_path: str) -> dict[str, Any]:
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM conversations) AS total,"
            " (SELECT COUNT(*) FROM conversations WHERE status='done') AS done,"
            " (SELECT COUNT(*) FROM conversations WHERE status='collecting') AS collecting,"
            " (SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND failed_at IS NULL) AS pending,"
            " (SELECT COUNT(*) FROM outbox WHERE failed_at IS NOT NULL) AS failed"
        ).fetchone()
        return dict(row)
    finally:
        con.close()


def failed_messages(db_path: str, limit: int = 20) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, account_id, sender_id, body, attempts, last_error, failed_at"
            " FROM outbox WHERE failed_at IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]
    finally:
        con.close()