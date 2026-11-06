"""Optional Telegram notification for staff when a lead is captured.

Delivery goes through the same outbox as Instagram replies, so a Telegram outage
never loses the lead - it just retries.
"""

from __future__ import annotations

import json
import logging

import httpx

from .config import Settings
from .sender import PermanentSendError, SendError

logger = logging.getLogger("leadsbot.notify")


class TelegramNotifier:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def format_lead(self, account_id: str, payload: str) -> str:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {}
        account = self.settings.accounts.get(account_id)
        lines = [
            "🆕 Yeni lid",
            f"Hesab: {account.name if account else account_id}",
            f"Ad: {data.get('name') or '-'}",
        ]
        if data.get("phone"):
            lines.append(f"Telefon: {data['phone']}")
        if data.get("instagram_id"):
            lines.append(f"Instagram ID: {data['instagram_id']}")
        return "\n".join(lines)

    async def notify(self, account_id: str, payload: str) -> None:
        if not self.settings.notifications_enabled:
            raise PermanentSendError("telegram notifications are not configured")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {}
        # Each outbox row targets exactly one chat, so retries never duplicate
        # delivery to the recipients that already succeeded.
        chat_id = str(data.get("chat_id") or (self.settings.telegram_chat_ids or [""])[0])
        if not chat_id:
            raise PermanentSendError("no telegram chat id in payload")
        text = self.format_lead(account_id, payload)
        url = f"https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage"
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout, transport=self.transport
        ) as client:
            try:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                )
            except httpx.HTTPError as exc:
                raise SendError(f"telegram network error: {exc}") from exc
        if 200 <= response.status_code < 300:
            logger.info("lead notification sent to chat %s", chat_id)
            return
        detail = f"telegram {response.status_code}: {response.text[:200]}"
        if response.status_code == 429 or response.status_code >= 500:
            raise SendError(detail)
        raise PermanentSendError(detail)