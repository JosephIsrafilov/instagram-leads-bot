"""Telegram lead notifications: formatting, delivery, retries, disabled state."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from leadsbot.config import load_settings
from leadsbot.notify import TelegramNotifier
from leadsbot.sender import PermanentSendError, SendError


@pytest.fixture
def tg_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "tg.sqlite3"))
    monkeypatch.setenv("IG_ACCOUNTS_JSON", '{"123": {"name": "Basic Academy", "access_token": "t"}}')
    monkeypatch.setenv("LEADS_TELEGRAM_TOKEN", "bot-token")
    monkeypatch.setenv("LEADS_TELEGRAM_CHAT_IDS", "111,222")
    return load_settings()


PAYLOAD = json.dumps({"name": "Elvin", "phone": "+994501234567", "instagram_id": "900001"})


def test_message_contains_lead_and_account(tg_settings):
    text = TelegramNotifier(tg_settings).format_lead("123", PAYLOAD)
    assert "Basic Academy" in text
    assert "Elvin" in text
    assert "+994501234567" in text
    assert "900001" in text


def test_sends_to_every_chat(tg_settings):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert "bot-token" in str(request.url)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(tg_settings, transport=httpx.MockTransport(handler))
    asyncio.run(notifier.notify("123", PAYLOAD))
    assert [item["chat_id"] for item in seen] == ["111", "222"]
    assert "Elvin" in seen[0]["text"]


@pytest.mark.parametrize("code", [429, 500, 503])
def test_transient_telegram_errors_are_retryable(tg_settings, code):
    notifier = TelegramNotifier(
        tg_settings, transport=httpx.MockTransport(lambda _r: httpx.Response(code))
    )
    with pytest.raises(SendError) as exc:
        asyncio.run(notifier.notify("123", PAYLOAD))
    assert not isinstance(exc.value, PermanentSendError)


def test_telegram_400_is_permanent(tg_settings):
    notifier = TelegramNotifier(
        tg_settings, transport=httpx.MockTransport(lambda _r: httpx.Response(400))
    )
    with pytest.raises(PermanentSendError):
        asyncio.run(notifier.notify("123", PAYLOAD))


def test_disabled_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "tg2.sqlite3"))
    monkeypatch.setenv("IG_ACCOUNTS_JSON", "{}")
    monkeypatch.delenv("LEADS_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("LEADS_TELEGRAM_CHAT_IDS", raising=False)
    settings = load_settings()
    assert settings.notifications_enabled is False
    with pytest.raises(PermanentSendError):
        asyncio.run(TelegramNotifier(settings).notify("123", PAYLOAD))


# --- integration through the outbox -----------------------------------------
def test_lead_is_enqueued_and_delivered(settings, monkeypatch):
    from leadsbot import storage
    from leadsbot.worker import OutboxWorker

    monkeypatch.setenv("LEADS_TELEGRAM_TOKEN", "bot-token")
    monkeypatch.setenv("LEADS_TELEGRAM_CHAT_IDS", "111")
    configured = load_settings()

    storage.intake(
        settings.database_path,
        "17841400000000001",
        "u1",
        "m1",
        "Elvin 0501234567",
        notify_telegram=True,
    )
    con = storage.connect(settings.database_path)
    try:
        channels = [row["channel"] for row in con.execute("SELECT channel FROM outbox ORDER BY id")]
    finally:
        con.close()
    assert channels == ["instagram", "telegram"]

    sent: list[str] = []

    class FakeNotifier:
        def __init__(self, _settings): pass

        async def notify(self, account_id, payload):
            sent.append(payload)

    worker = OutboxWorker(
        sender_factory=lambda _s: _NoopSender(),
        telegram_factory=FakeNotifier,
    )
    assert asyncio.run(worker.run_once(configured)) == 2
    assert len(sent) == 1 and "Elvin" in sent[0]
    assert storage.stats(settings.database_path)["pending"] == 0


class _NoopSender:
    async def send(self, account, sender_id, text):
        return None