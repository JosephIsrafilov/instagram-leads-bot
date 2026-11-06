"""Settings loaded from environment variables.

Everything is read lazily so tests (and a restarted process) pick up `.env` changes
without re-importing modules.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

SEND_MODES = {"api", "console"}


class ConfigError(RuntimeError):
    """Configuration is missing or malformed; do not start serving."""


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    access_token: str


@dataclass(frozen=True)
class Settings:
    database_path: str
    accounts: dict[str, Account]
    app_secret: str
    verify_token: str
    api_version: str
    send_mode: str
    admin_user: str
    admin_password: str
    worker_enabled: bool
    poll_interval: float
    max_attempts: int
    max_hints: int
    request_timeout: float
    telegram_token: str
    telegram_chat_ids: tuple[str, ...]

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_ids)


def load_accounts() -> dict[str, Account]:
    raw = os.environ.get("IG_ACCOUNTS_JSON", "").strip() or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("IG_ACCOUNTS_JSON is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("IG_ACCOUNTS_JSON must be a JSON object")
    accounts: dict[str, Account] = {}
    for account_id, value in parsed.items():
        if not isinstance(value, dict):
            raise ConfigError(f"IG_ACCOUNTS_JSON[{account_id!r}] must be an object")
        token = str(value.get("access_token", "")).strip()
        if not token:
            raise ConfigError(f"IG_ACCOUNTS_JSON[{account_id!r}] is missing access_token")
        accounts[str(account_id)] = Account(
            account_id=str(account_id),
            name=str(value.get("name") or account_id),
            access_token=token,
        )
    return accounts


def load_settings() -> Settings:
    send_mode = (os.environ.get("IG_SEND_MODE", "api").strip().lower() or "api")
    if send_mode not in SEND_MODES:
        raise ConfigError(f"IG_SEND_MODE must be one of {sorted(SEND_MODES)}, got {send_mode!r}")
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if admin_password and len(admin_password) < 8:
        raise ConfigError("ADMIN_PASSWORD must be at least 8 characters")
    return Settings(
        database_path=os.environ.get("DATABASE_PATH", "./leads.sqlite3"),
        accounts=load_accounts(),
        app_secret=os.environ.get("META_APP_SECRET", "").strip(),
        verify_token=os.environ.get("META_VERIFY_TOKEN", "").strip(),
        api_version=os.environ.get("META_API_VERSION", "v26.0").strip(),
        send_mode=send_mode,
        admin_user=os.environ.get("ADMIN_USER", "admin").strip() or "admin",
        admin_password=admin_password,
        worker_enabled=_bool("WORKER_ENABLED", True),
        poll_interval=max(0.2, _float("WORKER_POLL_INTERVAL", 2.0)),
        max_attempts=max(1, _int("OUTBOX_MAX_ATTEMPTS", 6)),
        max_hints=max(1, _int("BOT_MAX_HINTS", 3)),
        request_timeout=max(1.0, _float("META_REQUEST_TIMEOUT", 12.0)),
        telegram_token=os.environ.get("LEADS_TELEGRAM_TOKEN", "").strip(),
        telegram_chat_ids=tuple(
            chat.strip()
            for chat in os.environ.get("LEADS_TELEGRAM_CHAT_IDS", "").split(",")
            if chat.strip()
        ),
    )