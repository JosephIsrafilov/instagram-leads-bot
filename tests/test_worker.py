"""Outbox durability: retries, backoff, permanent failures, lease safety."""

from __future__ import annotations

import asyncio

import pytest

from leadsbot import logic, storage
from leadsbot.config import load_settings
from leadsbot.sender import PermanentSendError, SendError
from leadsbot.worker import OutboxWorker, backoff


class RecordingSender:
    def __init__(self, failures: int = 0, permanent: bool = False) -> None:
        self.failures = failures
        self.permanent = permanent
        self.sent: list[str] = []

    async def send(self, account, sender_id, text):
        if self.failures > 0:
            self.failures -= 1
            raise PermanentSendError("400 rejected") if self.permanent else SendError("503 up")
        self.sent.append(text)


def make_worker(sender) -> OutboxWorker:
    return OutboxWorker(sender_factory=lambda _settings: sender)


def enqueue_one(settings, text: str = "Salam") -> None:
    assert storage.intake(settings.database_path, "17841400000000001", "u1", "mid-1", text)


def force_due(db_path: str) -> None:
    con = storage.connect(db_path)
    try:
        con.execute(
            "UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00', locked_until=NULL"
        )
        con.commit()
    finally:
        con.close()


def test_success_marks_sent(settings):
    enqueue_one(settings)
    sender = RecordingSender()
    assert asyncio.run(make_worker(sender).run_once(settings)) == 1
    assert sender.sent == [logic.GREETING]
    counters = storage.stats(settings.database_path)
    assert counters["pending"] == 0 and counters["failed"] == 0


def test_transient_failure_backs_off_then_succeeds(settings):
    enqueue_one(settings)
    sender = RecordingSender(failures=1)
    worker = make_worker(sender)

    assert asyncio.run(worker.run_once(settings)) == 1
    assert sender.sent == []

    con = storage.connect(settings.database_path)
    try:
        row = con.execute("SELECT attempts, sent_at, failed_at, last_error FROM outbox").fetchone()
    finally:
        con.close()
    assert row["attempts"] == 1 and row["sent_at"] is None and row["failed_at"] is None
    assert "503" in row["last_error"]

    # Backoff keeps it out of the very next batch.
    assert asyncio.run(worker.run_once(settings)) == 0
    assert sender.sent == []

    force_due(settings.database_path)
    assert asyncio.run(worker.run_once(settings)) == 1
    assert sender.sent == [logic.GREETING]
    assert storage.stats(settings.database_path)["pending"] == 0


def test_permanent_failure_is_not_retried(settings):
    enqueue_one(settings)
    sender = RecordingSender(failures=1, permanent=True)
    assert asyncio.run(make_worker(sender).run_once(settings)) == 1
    failures = storage.failed_messages(settings.database_path)
    assert len(failures) == 1
    assert "400" in failures[0]["last_error"]
    assert asyncio.run(make_worker(sender).run_once(settings)) == 0


def test_retry_gives_up_after_max_attempts(settings, monkeypatch):
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "1")
    settings = load_settings()
    enqueue_one(settings)
    sender = RecordingSender(failures=5)
    assert asyncio.run(make_worker(sender).run_once(settings)) == 1
    failures = storage.failed_messages(settings.database_path)
    assert len(failures) == 1
    assert failures[0]["attempts"] == 1


def test_claiming_is_exclusive(settings):
    enqueue_one(settings)
    assert len(storage.claim_due(settings.database_path)) == 1
    assert storage.claim_due(settings.database_path) == []


def test_unknown_account_fails_fast(settings, monkeypatch):
    monkeypatch.setenv(
        "IG_ACCOUNTS_JSON",
        '{"17841400000000002": {"name": "Affidavit", "access_token": "t"}}',
    )
    settings = load_settings()
    storage.init_db(settings.database_path)
    enqueue_one(settings)
    assert asyncio.run(make_worker(RecordingSender()).run_once(settings)) == 1
    failures = storage.failed_messages(settings.database_path)
    assert "unknown account" in failures[0]["last_error"]


@pytest.mark.parametrize("attempts, expected", [(1, 5.0), (2, 10.0), (3, 20.0), (20, 600.0)])
def test_backoff_grows_and_caps(attempts, expected):
    assert backoff(attempts) == expected


def test_outbox_is_fifo_per_conversation(settings):
    db = settings.database_path
    storage.intake(db, "17841400000000001", "u1", "m1", "Salam")
    storage.intake(db, "17841400000000001", "u1", "m2", "Elvin")

    sender = RecordingSender(failures=1)
    worker = make_worker(sender)

    asyncio.run(worker.run_once(settings))
    assert sender.sent == []  # greeting failed, so the follow-up must wait

    force_due(db)
    asyncio.run(worker.run_once(settings))
    assert sender.sent == [logic.GREETING]

    force_due(db)
    asyncio.run(worker.run_once(settings))
    assert sender.sent == [logic.GREETING, logic.ASK_PHONE]


def test_stuck_conversation_does_not_block_others(settings):
    db = settings.database_path

    class SelectiveSender:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send(self, account, sender_id, text):
            if sender_id == "blocked":
                raise SendError("503 up")
            self.sent.append((sender_id, text))

    storage.intake(db, "17841400000000001", "blocked", "m-blocked", "Salam")
    storage.intake(db, "17841400000000001", "healthy", "m-healthy", "Salam")

    sender = SelectiveSender()
    assert asyncio.run(make_worker(sender).run_once(settings)) == 2
    assert [who for who, _ in sender.sent] == ["healthy"]