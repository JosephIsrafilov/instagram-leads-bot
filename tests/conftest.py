"""Shared fixtures: temp database, fake accounts, unsigned/unsigned requests."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as bot  # noqa: E402
from leadsbot import storage  # noqa: E402
from leadsbot.config import Settings, load_settings  # noqa: E402

ACCOUNT_A = "17841400000000001"
ACCOUNT_B = "17841400000000002"
APP_SECRET = "test_secret"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv(
        "IG_ACCOUNTS_JSON",
        json.dumps(
            {
                ACCOUNT_A: {"name": "Basic Academy", "access_token": "token-a"},
                ACCOUNT_B: {"name": "Affidavit", "access_token": "token-b"},
            }
        ),
    )
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("META_VERIFY_TOKEN", "test_verify")
    monkeypatch.setenv("IG_SEND_MODE", "console")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("WORKER_ENABLED", "0")
    monkeypatch.delenv("BOT_MAX_HINTS", raising=False)
    monkeypatch.delenv("OUTBOX_MAX_ATTEMPTS", raising=False)
    resolved = load_settings()
    storage.init_db(resolved.database_path)
    return resolved


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(bot.app)


def signed_raw(payload: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": signature, "content-type": "application/json"}


def signed_payload(account: str, sender: str, mid: str, text: str) -> tuple[bytes, dict[str, str]]:
    return signed_raw(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": account,
                    "time": 1700000000,
                    "messaging": [
                        {
                            "sender": {"id": sender},
                            "recipient": {"id": account},
                            "timestamp": 1700000000000,
                            "message": {"mid": mid, "text": text},
                        }
                    ],
                }
            ],
        }
    )