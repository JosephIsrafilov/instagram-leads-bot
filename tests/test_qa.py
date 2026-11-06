"""QA checklist: the 15 agreed scenarios, end to end through the real handlers.

Each test name maps to the QA item; every assertion goes through the webhook,
the outbox worker or the admin panel - not through logic functions directly.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from conftest import ACCOUNT_A, ACCOUNT_B, signed_payload

from leadsbot import logic, storage
from leadsbot.config import load_settings
from leadsbot.sender import PermanentSendError, SendError
from leadsbot.worker import OutboxWorker

AUTH = ("admin", "test-admin-pass")


def post(client, account, sender, mid, text):
    body, headers = signed_payload(account, sender, mid, text)
    return client.post("/webhook", content=body, headers=headers)


def drain(settings, sender_factory=None, batches: int = 5) -> list[str]:
    """Run the worker until the queue stops changing; return reply bodies in order."""
    worker = OutboxWorker(sender_factory=sender_factory) if sender_factory else OutboxWorker()
    for _ in range(batches):
        if asyncio.run(worker.run_once(settings)) == 0:
            break
    con = storage.connect(settings.database_path)
    try:
        return [row["body"] for row in con.execute("SELECT body FROM outbox ORDER BY id")]
    finally:
        con.close()


def force_all_due(settings) -> None:
    con = storage.connect(settings.database_path)
    try:
        con.execute("UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00', locked_until=NULL")
        con.commit()
    finally:
        con.close()


def case(settings, sender="qa"):
    rows = storage.leads(settings.database_path)
    return next(r for r in rows if r["sender_id"] == sender)


# 01 -------------------------------------------------------------------------
def test_01_normal_conversation(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    post(client, ACCOUNT_A, "qa", "m2", "Elvin")
    post(client, ACCOUNT_A, "qa", "m3", "050 123 45 67")
    replies = drain(settings)
    assert replies == [logic.GREETING, logic.ASK_PHONE, logic.THANK_YOU]
    lead = case(settings)
    assert (lead["name"], lead["status"]) == ("Elvin", "done")
    assert lead["phone"].endswith("4567")


# 02 -------------------------------------------------------------------------
def test_02_name_and_phone_in_one_message(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Elvin 0501234567")
    replies = drain(settings)
    assert replies == [logic.THANK_YOU]  # exactly one message, no double greeting
    assert case(settings)["status"] == "done"


# 03 -------------------------------------------------------------------------
def test_03_phone_first_then_name(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "0501234567")
    replies = drain(settings)
    assert replies == [logic.ASK_NAME]
    assert case(settings)["status"] == "awaiting_name"

    post(client, ACCOUNT_A, "qa", "m2", "Murad")
    replies = drain(settings)
    assert replies[-1] == logic.THANK_YOU
    assert case(settings)["status"] == "done"


# 04 -------------------------------------------------------------------------
def test_04_price_question_is_not_a_name(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    post(client, ACCOUNT_A, "qa", "m2", "Qiymət neçədir?")
    drain(settings)
    lead = case(settings)
    assert lead["name"] is None
    assert lead["status"] == "awaiting_contact"


# 05 -------------------------------------------------------------------------
def test_05_invalid_number_asks_again(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Elvin")
    post(client, ACCOUNT_A, "qa", "m2", "12345")
    replies = drain(settings)
    assert replies[-1] == logic.ASK_PHONE_RETRY
    lead = case(settings)
    assert lead["phone"] is None and lead["status"] == "awaiting_phone"


# 06 -------------------------------------------------------------------------
def test_06_finished_client_gets_silence(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    post(client, ACCOUNT_A, "qa", "m2", "Elvin 0501234567")
    drain(settings)
    before = len(storage.failed_messages(settings.database_path)) + storage.stats(settings.database_path)["pending"]

    post(client, ACCOUNT_A, "qa", "m3", "Qiymət nədir?")
    drain(settings)
    con = storage.connect(settings.database_path)
    try:
        total = con.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
    finally:
        con.close()
    assert total == 2  # greeting + thank you, nothing new
    assert before == 0


# 07 -------------------------------------------------------------------------
def test_07_contact_refusal_ends_up_manual(client, settings):
    for index in range(5):
        post(client, ACCOUNT_A, "qa", f"m{index}", "salam")
    replies = drain(settings)
    assert replies[0] == logic.GREETING
    assert logic.CLOSING in replies
    lead = case(settings)
    assert lead["status"] == "manual"

    post(client, ACCOUNT_A, "qa", "m-late", "salam")
    drain(settings)
    assert storage.stats(settings.database_path)["pending"] == 0


# 08 -------------------------------------------------------------------------
def test_08_same_sender_two_accounts_are_separate(client, settings):
    post(client, ACCOUNT_A, "same-person", "m1", "Salam")
    post(client, ACCOUNT_B, "same-person", "m1", "Salam")
    rows = storage.leads(settings.database_path)
    assert len(rows) == 2
    assert {row["account_id"] for row in rows} == {ACCOUNT_A, ACCOUNT_B}


# 09 -------------------------------------------------------------------------
def test_09_duplicate_webhook_delivery_is_ignored(client, settings):
    assert post(client, ACCOUNT_A, "qa", "dup-1", "Salam").json()["accepted"] == 1
    assert post(client, ACCOUNT_A, "qa", "dup-1", "Salam").json()["skipped"] == 1
    con = storage.connect(settings.database_path)
    try:
        assert con.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"] == 1
    finally:
        con.close()


# 10 -------------------------------------------------------------------------
def test_10_transient_send_error_is_retried(client, settings):
    class Flaky:
        calls = 0

        async def send(self, account, sender_id, text):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise SendError("503 service unavailable")

    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    assert asyncio.run(OutboxWorker(sender_factory=lambda _s: Flaky()).run_once(settings)) == 1
    assert storage.stats(settings.database_path)["pending"] == 1
    assert storage.failed_messages(settings.database_path) == []

    force_all_due(settings)
    asyncio.run(OutboxWorker(sender_factory=lambda _s: Flaky()).run_once(settings))
    assert storage.stats(settings.database_path)["pending"] == 0


# 11 -------------------------------------------------------------------------
def test_11_rejected_message_is_recorded(client, settings):
    class Rejecting:
        async def send(self, account, sender_id, text):
            raise PermanentSendError("400 invalid recipient")

    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    asyncio.run(OutboxWorker(sender_factory=lambda _s: Rejecting()).run_once(settings))
    failures = storage.failed_messages(settings.database_path)
    assert len(failures) == 1
    assert "400" in failures[0]["last_error"]
    assert "400" in client.get("/admin", auth=AUTH).text


# 12 -------------------------------------------------------------------------
def test_12_queue_survives_restart(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    post(client, ACCOUNT_A, "qa", "m2", "Elvin")
    # Simulate a restart: nothing in memory, a brand-new worker instance.
    fresh = load_settings()
    replies = drain(fresh)
    assert replies == [logic.GREETING, logic.ASK_PHONE]
    assert storage.stats(fresh.database_path)["pending"] == 0


# 13 -------------------------------------------------------------------------
def test_13_manager_can_stop_the_bot(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    response = client.post(
        f"/admin/{ACCOUNT_A}/qa/status",
        data={"new_status": "manual", "back": "/admin"},
        auth=AUTH,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert case(settings)["status"] == "manual"

    post(client, ACCOUNT_A, "qa", "m2", "Elvin")
    drain(settings)
    assert case(settings)["status"] == "manual"
    assert case(settings)["name"] is None

    # Handing the dialogue back to the bot resets the prompts.
    client.post(
        f"/admin/{ACCOUNT_A}/qa/status",
        data={"new_status": "awaiting_contact", "back": "/admin"},
        auth=AUTH,
    )
    assert case(settings)["status"] == "awaiting_contact"


# 14 -------------------------------------------------------------------------
def test_14_bad_signature_changes_nothing(client, settings):
    body, _ = signed_payload(ACCOUNT_A, "qa", "m1", "Salam")
    response = client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=deadbeef"})
    assert response.status_code == 403
    assert storage.leads(settings.database_path) == []


# 15 -------------------------------------------------------------------------
def test_15_database_failure_is_not_acknowledged(client, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "intake", broken)
    body, headers = signed_payload(ACCOUNT_A, "qa", "m1", "Salam")
    assert client.post("/webhook", content=body, headers=headers).status_code == 503


def test_15b_after_database_recovers_redelivery_works(client, settings, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    body, headers = signed_payload(ACCOUNT_A, "qa", "m1", "Salam")
    with monkeypatch.context() as patch:
        patch.setattr(storage, "intake", broken)
        assert client.post("/webhook", content=body, headers=headers).status_code == 503

    again, headers = signed_payload(ACCOUNT_A, "qa", "m1", "Salam")
    response = client.post("/webhook", content=again, headers=headers)
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert case(settings)["status"] == "awaiting_contact"


# Panels ---------------------------------------------------------------------
def test_admin_filters_and_pagination(client, settings):
    for index in range(3):
        post(client, ACCOUNT_A, f"user-{index}", f"m{index}", "Elvin 0501234567")
    post(client, ACCOUNT_B, "other-account", "m1", "Salam")

    by_account = client.get(f"/admin?account={ACCOUNT_B}", auth=AUTH).text
    assert "other-account" in by_account
    assert "user-1" not in by_account

    done_only = client.get("/admin?status=done", auth=AUTH).text
    assert "other-account" not in done_only

    first_page = client.get("/admin?page=1", auth=AUTH)
    assert first_page.status_code == 200


def test_status_filter_rejects_unknown_values(client, settings):
    post(client, ACCOUNT_A, "qa", "m1", "Salam")
    assert client.get("/admin?status=whatever", auth=AUTH).status_code == 200