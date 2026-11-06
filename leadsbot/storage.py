"""SQLite persistence: conversations, processed messages, outbox.

The outbox is the durability boundary: a reply is written in the same
transaction as the conversation state it belongs to, so a crash between
"decided to answer" and "asked Instagram to send" can never lose a message.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .logic import STATUS_AWAITING_CONTACT, STATUS_DONE, Plan, plan_reply

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    account_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_contact',
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
    channel TEXT NOT NULL DEFAULT 'instagram',
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
    "outbox": {"channel": "TEXT NOT NULL DEFAULT 'instagram'"},
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
        # First prototype used 'new'/'collecting'; the dialogue is now an explicit
        # state machine and those two both mean "we still know nothing".
        con.execute(
            "UPDATE conversations SET status='awaiting_contact'"
            " WHERE status IN ('new','collecting')"
        )
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
    notify_chat_ids: tuple[str, ...] = (),
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
        previous_status = row["status"] if row is not None else None
        if row is None:
            plan = plan_reply(None, None, STATUS_AWAITING_CONTACT, 0, text, max_hints)
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
        if (
            notify_chat_ids
            and plan.status == STATUS_DONE
            and previous_status != STATUS_DONE
            and plan.phone
        ):
            # Only the transition into "done" notifies staff: later messages from the
            # same customer must not spam the managers. One outbox row per recipient
            # so a failure for one chat cannot duplicate delivery to another.
            for chat_id in notify_chat_ids:
                enqueue(
                    con,
                    account_id,
                    sender_id,
                    json.dumps(
                        {
                            "name": plan.name,
                            "phone": plan.phone,
                            "instagram_id": sender_id,
                            "chat_id": chat_id,
                        },
                        ensure_ascii=False,
                    ),
                    stamp,
                    channel="telegram",
                )
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
    channel: str = "instagram",
) -> int:
    created_at = created_at or now()
    cursor = con.execute(
        "INSERT INTO outbox (account_id, sender_id, body, channel, created_at, next_attempt_at)"
        " VALUES (?,?,?,?,?,?)",
        (account_id, sender_id, body, channel, created_at, created_at),
    )
    return int(cursor.lastrowid)


def claim_due(db_path: str, limit: int = 20, lease_seconds: int = 120) -> list[dict[str, Any]]:
    """Reserve due outbox rows so two workers (or a reload) cannot double-send."""
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        stamp = now()
        rows = con.execute(
            "SELECT id, account_id, sender_id, body, channel, attempts FROM outbox o"
            " WHERE o.sent_at IS NULL AND o.failed_at IS NULL"
            "   AND o.next_attempt_at <= ?"
            "   AND (o.locked_until IS NULL OR o.locked_until <= ?)"
            # FIFO per conversation for customer-visible replies: never send message N+1
            # while N is still queued. Notifications are independent recipients.
            "   AND (o.channel <> 'instagram' OR o.id = (SELECT MIN(x.id) FROM outbox x"
            "               WHERE x.account_id=o.account_id AND x.sender_id=o.sender_id"
            "                 AND x.channel=o.channel"
            "                 AND x.sent_at IS NULL AND x.failed_at IS NULL))"
            " ORDER BY o.id LIMIT ?",
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


def cancel_pending(db_path: str, account_id: str, sender_id: str, reason: str) -> int:
    """Drop queued replies for one dialogue - used when staff take over."""
    con = connect(db_path)
    try:
        cursor = con.execute(
            "UPDATE outbox SET failed_at=?, locked_until=NULL, last_error=?"
            " WHERE account_id=? AND sender_id=? AND sent_at IS NULL AND failed_at IS NULL",
            (now(), reason[:500], account_id, sender_id),
        )
        con.commit()
        return int(cursor.rowcount)
    finally:
        con.close()


def leads(
    db_path: str,
    status: str | None = None,
    account_id: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        where, params = _lead_filters(status, account_id)
        sql = f"SELECT * FROM conversations{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def count_leads(db_path: str, status: str | None = None, account_id: str | None = None) -> int:
    con = connect(db_path)
    try:
        where, params = _lead_filters(status, account_id)
        row = con.execute(f"SELECT COUNT(*) AS n FROM conversations{where}", params).fetchone()
        return int(row["n"])
    finally:
        con.close()


def _lead_filters(status: str | None, account_id: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if account_id:
        clauses.append("account_id=?")
        params.append(account_id)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def set_status(db_path: str, account_id: str, sender_id: str, status: str) -> bool:
    """Staff action: take over a dialogue (manual) or hand it back to the bot."""
    con = connect(db_path)
    try:
        cursor = con.execute(
            "UPDATE conversations SET status=?, updated_at=?,"
            " hints=CASE WHEN ?='awaiting_contact' THEN 0 ELSE hints END"
            " WHERE account_id=? AND sender_id=?",
            (status, now(), status, account_id, sender_id),
        )
        con.commit()
        return cursor.rowcount > 0
    finally:
        con.close()


def stats(db_path: str) -> dict[str, Any]:
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM conversations) AS total,"
            " (SELECT COUNT(*) FROM conversations WHERE status='done') AS done,"
            " (SELECT COUNT(*) FROM conversations"
            "   WHERE status IN ('awaiting_contact','awaiting_name','awaiting_phone')) AS waiting,"
            " (SELECT COUNT(*) FROM conversations WHERE status='manual') AS manual,"
            " (SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND failed_at IS NULL) AS pending,"
            " (SELECT COUNT(*) FROM outbox WHERE failed_at IS NOT NULL"
            "   AND COALESCE(last_error,'') NOT LIKE 'cancelled%') AS failed,"
            " (SELECT COUNT(*) FROM outbox WHERE COALESCE(last_error,'') LIKE 'cancelled%') AS cancelled"
        ).fetchone()
        return dict(row)
    finally:
        con.close()


def failed_messages(db_path: str, limit: int = 20) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, account_id, sender_id, body, attempts, last_error, failed_at"
            " FROM outbox WHERE failed_at IS NOT NULL"
            "   AND COALESCE(last_error,'') NOT LIKE 'cancelled%' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]
    finally:
        con.close()