"""Outbox dispatcher: delivers queued replies with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from . import storage
from .config import Settings, load_settings
from .notify import TelegramNotifier
from .sender import InstagramSender, PermanentSendError, SendError

logger = logging.getLogger("leadsbot.worker")

MAX_BACKOFF_SECONDS = 600.0
BASE_BACKOFF_SECONDS = 5.0


def backoff(attempts: int) -> float:
    return min(BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS)


class OutboxWorker:
    def __init__(
        self,
        sender_factory: Callable[[Settings], Any] = InstagramSender,
        telegram_factory: Callable[[Settings], Any] | None = None,
        batch_size: int = 20,
    ) -> None:
        self.sender_factory = sender_factory
        self.telegram_factory = telegram_factory or TelegramNotifier
        self.batch_size = batch_size

    async def run_once(self, settings: Settings | None = None) -> int:
        """Deliver one batch of due messages. Returns how many were attempted."""
        settings = settings or load_settings()
        rows = storage.claim_due(settings.database_path, limit=self.batch_size)
        if not rows:
            return 0
        sender = self.sender_factory(settings)
        notifier = self.telegram_factory(settings)
        for row in rows:
            account = settings.accounts.get(row["account_id"])
            is_telegram = row["channel"] == "telegram"
            if account is None and not is_telegram:
                storage.mark_failed(
                    settings.database_path,
                    row["id"],
                    f"unknown account {row['account_id']}",
                )
                logger.error("outbox %s: account %s is not configured", row["id"], row["account_id"])
                continue
            attempts = int(row["attempts"]) + 1
            try:
                if is_telegram:
                    await notifier.notify(row["account_id"], row["body"])
                else:
                    await sender.send(account, row["sender_id"], row["body"])
            except PermanentSendError as exc:
                storage.mark_failed(settings.database_path, row["id"], str(exc), attempts)
                logger.error("outbox %s failed permanently: %s", row["id"], exc)
            except SendError as exc:
                if attempts >= settings.max_attempts:
                    storage.mark_failed(settings.database_path, row["id"], str(exc), attempts)
                    logger.error("outbox %s exhausted %s attempts: %s", row["id"], attempts, exc)
                else:
                    delay = backoff(attempts)
                    storage.mark_retry(settings.database_path, row["id"], str(exc), attempts, delay)
                    logger.warning(
                        "outbox %s attempt %s failed, retrying in %.0fs: %s",
                        row["id"], attempts, delay, exc,
                    )
            except Exception as exc:  # unexpected bug must not kill the loop
                if attempts >= settings.max_attempts:
                    storage.mark_failed(settings.database_path, row["id"], repr(exc), attempts)
                else:
                    storage.mark_retry(
                        settings.database_path, row["id"], repr(exc), attempts, backoff(attempts)
                    )
                logger.exception("outbox %s unexpected error", row["id"])
            else:
                storage.mark_sent(settings.database_path, row["id"])
                logger.info("outbox %s sent to %s", row["id"], row["sender_id"])
        return len(rows)

    async def run_forever(self) -> None:
        logger.info("outbox worker started")
        failures = 0
        try:
            while True:
                try:
                    settings = load_settings()
                    interval = settings.poll_interval
                    await self.run_once(settings)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    interval = 5.0
                    if failures == 1:
                        logger.exception("outbox worker iteration failed")
                    else:
                        logger.warning("outbox worker still failing (%s in a row)", failures)
                    # The database file may have been replaced or emptied. Rebuilding
                    # the schema is idempotent and stops an endless error loop.
                    try:
                        storage.init_db(load_settings().database_path)
                    except Exception:
                        logger.debug("schema repair failed", exc_info=True)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("outbox worker stopped")
            raise