"""Local load test: N unique conversations through the real webhook and worker.

    python tools/loadtest.py --count 500

Uses the ASGI app in-process (no network, no Instagram), so it measures our own
code: webhook + SQLite + outbox + worker. Verifies that every conversation stays
separate and that nothing is lost or duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ACCOUNTS = {
    "17841400000000001": {"name": "Basic Academy", "access_token": "load-1"},
    "17841400000000002": {"name": "Affidavit", "access_token": "load-2"},
}
SECRET = "load_secret"


def configure(db_path: str) -> None:
    os.environ.update(
        DATABASE_PATH=db_path,
        IG_ACCOUNTS_JSON=json.dumps(ACCOUNTS),
        META_APP_SECRET=SECRET,
        META_VERIFY_TOKEN="load_verify",
        IG_SEND_MODE="console",
        WORKER_ENABLED="0",
        LOG_LEVEL="WARNING",
    )


def signed(account: str, sender: str, mid: str, text: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": account,
                    "messaging": [
                        {
                            "sender": {"id": sender},
                            "recipient": {"id": account},
                            "message": {"mid": mid, "text": text},
                        }
                    ],
                }
            ],
        }
    ).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"content": body, "headers": {"x-hub-signature-256": signature}}


async def run(count: int, batch: int) -> dict:
    import httpx

    import app as web
    from leadsbot import storage
    from leadsbot.config import load_settings
    from leadsbot.worker import OutboxWorker

    settings = load_settings()
    storage.init_db(settings.database_path)
    transport = httpx.ASGITransport(app=web.app)

    latencies: list[float] = []
    statuses: dict[int, int] = {}

    async with httpx.AsyncClient(transport=transport, base_url="http://loadtest") as client:
        for offset in range(0, count, batch):
            requests = []
            for index in range(offset, min(offset + batch, count)):
                account = list(ACCOUNTS)[index % 2]
                payload = signed(account, f"load-{index}", f"mid-{index}", "Elvin 0501234567")
                requests.append(payload)
            started = time.perf_counter()
            responses = await asyncio.gather(
                *(client.post("/webhook", **payload) for payload in requests)
            )
            elapsed = time.perf_counter() - started
            latencies.extend([elapsed / len(responses) * 1000] * len(responses))
            for response in responses:
                statuses[response.status_code] = statuses.get(response.status_code, 0) + 1

    before = storage.stats(settings.database_path)
    drain_started = time.perf_counter()
    worker = OutboxWorker()
    sent_batches = 0
    while await worker.run_once(settings):
        sent_batches += 1
        if sent_batches > 10_000:  # safety valve
            break
    drain_seconds = time.perf_counter() - drain_started
    after = storage.stats(settings.database_path)

    con = storage.connect(settings.database_path)
    try:
        unique_conversations = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        unique_senders = con.execute("SELECT COUNT(DISTINCT sender_id) FROM conversations").fetchone()[0]
        per_account = {
            row["account_id"]: row["n"]
            for row in con.execute(
                "SELECT account_id, COUNT(*) AS n FROM conversations GROUP BY account_id"
            )
        }
        outbox_rows = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    finally:
        con.close()

    latencies.sort()
    return {
        "count": count,
        "statuses": statuses,
        "webhook_total_s": round(sum(latencies) / 1000, 2),
        "per_request_ms_p50": round(statistics.median(latencies), 2),
        "per_request_ms_p95": round(latencies[int(len(latencies) * 0.95) - 1], 2),
        "drain_s": round(drain_seconds, 2),
        "conversations": unique_conversations,
        "unique_senders": unique_senders,
        "per_account": per_account,
        "outbox_rows": outbox_rows,
        "pending_before_drain": before["pending"],
        "pending_after_drain": after["pending"],
        "failed": after["failed"],
        "ok": (
            unique_conversations == count
            and unique_senders == count
            and after["pending"] == 0
            and after["failed"] == 0
            and statuses.get(200) == count
            and outbox_rows == count  # one thank-you reply per conversation
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="In-process load test for the leads bot")
    parser.add_argument("--count", type=int, default=100, help="number of unique conversations")
    parser.add_argument("--batch", type=int, default=25, help="concurrent webhook requests")
    parser.add_argument("--keep", action="store_true", help="keep the temporary database")
    args = parser.parse_args()

    db_path = os.path.join(tempfile.mkdtemp(prefix="leads-load-"), "load.sqlite3")
    configure(db_path)
    result = asyncio.run(run(args.count, args.batch))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())