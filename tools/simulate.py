"""Send signed Instagram webhook payloads to a locally running bot.

    python tools/simulate.py                       # scripted demo conversation
    python tools/simulate.py "Salam" "Aysel" "055 333 44 22"
    python tools/simulate.py --sender 900001 --account 17841400000000001 "Salam"

Reads the bot's SQLite outbox afterwards, so you see exactly what the bot queued
to send (in console mode the server also logs each message).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_ACCOUNT = "17841400000000001"
DEFAULT_SECRET = "dev_secret"
DEFAULT_DB = "./dev.sqlite3"
DEMO_SCRIPT = ["Salam, kurslar haqqında məlumat almaq istəyirəm", "Elvin", "050 123 45 67"]


def load_dotenv_simple() -> None:
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def send(
    url: str, secret: str, account: str, sender: str, mid: str, text: str, username: str | None = None
) -> tuple[int, str]:
    sender_block: dict[str, object] = {"id": sender}
    if username:
        sender_block["username"] = username.lstrip("@")
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": account,
                "time": int(time.time()),
                "messaging": [
                    {
                        "sender": sender_block,
                        "recipient": {"id": account},
                        "timestamp": int(time.time() * 1000),
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url.rstrip("/") + "/webhook",
        data=body,
        headers={"Content-Type": "application/json", "x-hub-signature-256": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def health(base: str) -> str:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=5) as response:
            return response.read().decode()
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return f"unreachable ({exc})"


def show_replies(db_path: str, sender: str) -> None:
    if not Path(db_path).exists():
        print(f"\n(baza tapılmadı: {db_path})")
        return
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, body, sent_at, failed_at, attempts, last_error FROM outbox"
            " WHERE sender_id=? ORDER BY id",
            (sender,),
        ).fetchall()
        convo = con.execute(
            "SELECT name, phone, status FROM conversations WHERE sender_id=?", (sender,)
        ).fetchone()
    finally:
        con.close()

    print("\n--- bot cavabları ---")
    if not rows:
        print("(boşdur - worker hələ işləməyib?)")
    for row in rows:
        state = "göndərildi" if row["sent_at"] else ("xəta" if row["failed_at"] else "növbədə")
        print(f"[{state}] #{row['id']}: {row['body']}")
        if row["last_error"]:
            print(f"    xəta: {row['last_error']}")
    if convo:
        print(f"\n--- lid ---\nnömrə: {convo['phone']} | status: {convo['status']}")
    else:
        print("\n--- lid ---\n(heç biri)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate Instagram DMs against the local bot")
    parser.add_argument("texts", nargs="*", help="message texts (default: demo conversation)")
    parser.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--account", default=os.environ.get("SIM_ACCOUNT", DEFAULT_ACCOUNT))
    parser.add_argument("--sender", default=os.environ.get("SIM_SENDER", "900000001"))
    parser.add_argument(
        "--username",
        default=os.environ.get("SIM_USERNAME"),
        help="Instagram username to attach to the simulated sender",
    )
    parser.add_argument("--secret", default=os.environ.get("META_APP_SECRET", DEFAULT_SECRET))
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", DEFAULT_DB))
    parser.add_argument("--wait", type=float, default=3.0, help="seconds to wait for the worker")
    args = parser.parse_args()

    load_dotenv_simple()
    secret = os.environ.get("META_APP_SECRET") or args.secret
    texts = args.texts or DEMO_SCRIPT

    print(f"health: {health(args.url)}")
    stamp = str(int(time.time()))
    for index, text in enumerate(texts, start=1):
        mid = f"sim-{args.sender}-{stamp}-{index}"
        code, body = send(args.url, secret, args.account, args.sender, mid, text, args.username)
        print(f"\n> {text}\n  HTTP {code} {body}")

    if args.wait:
        time.sleep(args.wait)
    show_replies(args.db, args.sender)
    print(
        "\nqeyd: panel - "
        + args.url.rstrip("/")
        + "/admin"
    )
    print("csv: " + args.url.rstrip("/") + "/admin/leads.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())