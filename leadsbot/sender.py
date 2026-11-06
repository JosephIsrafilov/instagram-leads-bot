"""Instagram Graph API sender with explicit transient/permanent errors."""

from __future__ import annotations

import logging

import httpx

from .config import Account, Settings

logger = logging.getLogger("leadsbot.sender")


class SendError(RuntimeError):
    """Retryable failure (network, 429, 5xx)."""


class PermanentSendError(SendError):
    """Instagram rejected the message; retrying will not help."""


class InstagramSender:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        # Injectable transport keeps HTTP behaviour (200/400/429/timeout/...) testable
        # without touching the network.
        self.transport = transport

    async def send(self, account: Account, sender_id: str, text: str) -> None:
        if self.settings.send_mode == "console":
            logger.info("[console] %s -> %s: %s", account.name, sender_id, text)
            return

        url = (
            f"https://graph.instagram.com/{self.settings.api_version}"
            f"/{account.account_id}/messages"
        )
        payload = {"recipient": {"id": sender_id}, "message": {"text": text}}
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout, transport=self.transport
            ) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {account.access_token}"},
                )
        except httpx.HTTPError as exc:
            raise SendError(f"network error: {exc}") from exc

        if 200 <= response.status_code < 300:
            return
        detail = f"{response.status_code} {response.text[:300]}"
        if response.status_code == 429 or response.status_code >= 500:
            raise SendError(detail)
        raise PermanentSendError(detail)

    async def fetch_profile(self, account: Account, igsid: str) -> dict[str, str] | None:
        """Instagram profile behind an IGSID (username + display name).

        Best effort: any failure returns None and the dialogue continues. In console
        mode there is no API to ask, so local runs rely on the simulator instead.
        """
        if self.settings.send_mode == "console" or not igsid:
            return None
        url = f"https://graph.instagram.com/{self.settings.api_version}/{igsid}"
        try:
            async with httpx.AsyncClient(
                timeout=min(self.settings.request_timeout, 5.0), transport=self.transport
            ) as client:
                response = await client.get(
                    url,
                    params={"fields": "username,name"},
                    headers={"Authorization": f"Bearer {account.access_token}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("profile lookup failed for %s: %s", igsid, exc)
            return None
        if response.status_code != 200:
            logger.warning("profile lookup for %s returned %s", igsid, response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        username = str(data.get("username") or "").strip().lstrip("@")
        if not username:
            return None
        return {"username": username, "name": str(data.get("name") or "").strip()}