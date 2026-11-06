"""Webhook contract: verification, signature, deduplication, account routing."""

from __future__ import annotations

import json

from conftest import ACCOUNT_A, ACCOUNT_B, signed_payload, signed_raw

import pytest


def test_verification_handshake(client):
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "test_verify", "hub.challenge": "abc"},
    )
    assert response.status_code == 200
    assert response.text == "abc"
    assert client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong"}
    ).status_code == 403


def test_rejects_bad_signature_and_bad_json(client):
    assert client.post("/webhook", json={"object": "instagram"}).status_code == 403
    body, headers = signed_payload(ACCOUNT_A, "user1", "mid-1", "Salam")
    broken = body.replace(b"instagram", b"instagram\n")
    assert client.post("/webhook", content=broken, headers=headers).status_code == 403


def test_ignores_other_objects(client):
    body, headers = signed_payload(ACCOUNT_A, "user1", "mid-1", "Salam")
    payload = json.loads(body)
    payload["object"] = "page"
    raw, headers = signed_raw(payload)
    response = client.post("/webhook", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_two_accounts_are_independent_and_duplicates_ignored(client, settings):
    from leadsbot import storage

    def post(account, sender, mid, text):
        body, headers = signed_payload(account, sender, mid, text)
        return client.post("/webhook", content=body, headers=headers)

    assert post(ACCOUNT_A, "personA", "m1", "Salam").json()["accepted"] == 1
    assert post(ACCOUNT_A, "personA", "m2", "Elvin 050 123 45 67").json()["accepted"] == 1
    # Instagram re-delivers the same mid: must not be processed twice.
    assert post(ACCOUNT_A, "personA", "m2", "Elvin 050 123 45 67").json()["skipped"] == 1
    assert post(ACCOUNT_B, "personA", "m1", "Salam").json()["accepted"] == 1

    db = settings.database_path
    rows = {(row["account_id"], row["sender_id"]): row for row in storage.leads(db)}
    assert rows[(ACCOUNT_A, "personA")]["status"] == "done"
    assert rows[(ACCOUNT_A, "personA")]["phone"].endswith("4567")
    assert rows[(ACCOUNT_B, "personA")]["status"] == "awaiting_contact"
    assert storage.stats(db)["pending"] == 3


def test_unknown_account_is_skipped_not_stored(client, settings):
    from leadsbot import storage

    body, headers = signed_payload("999", "user9", "mid-9", "Salam")
    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["skipped"] == 1
    assert storage.leads(settings.database_path) == []


def test_echo_and_non_text_are_ignored(client, settings):
    for message in ({"mid": "e1", "is_echo": True, "text": "hi"}, {"mid": "e2", "attachments": []}):
        raw, headers = signed_raw(
            {"object": "instagram", "entry": [{"id": ACCOUNT_A, "messaging": [
                {"sender": {"id": "u"}, "message": message}]}]}
        )
        response = client.post("/webhook", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["skipped"] == 1

    from leadsbot import storage

    assert storage.leads(settings.database_path) == []


def test_missing_secret_returns_503(client, monkeypatch):
    monkeypatch.delenv("META_APP_SECRET")
    body, headers = signed_payload(ACCOUNT_A, "u", "m", "Salam")
    assert client.post("/webhook", content=body, headers=headers).status_code == 503


@pytest.mark.parametrize("path", ["/health", "/"])
def test_ops_endpoints(client, path):
    response = client.get(path)
    assert response.status_code == 200
    if path == "/health":
        assert response.json()["status"] == "ok"


def test_username_from_payload_is_stored(client, settings):
    from leadsbot import storage

    body, headers = signed_payload(ACCOUNT_A, "u-social", "m-1", "Salam", username="@ig_user")
    client.post("/webhook", content=body, headers=headers)
    lead = storage.get_lead(settings.database_path, ACCOUNT_A, "u-social")
    assert lead["username"] == "ig_user"

    # A later message without the field must not wipe the stored username.
    body, headers = signed_payload(ACCOUNT_A, "u-social", "m-2", "Elvin")
    client.post("/webhook", content=body, headers=headers)
    lead = storage.get_lead(settings.database_path, ACCOUNT_A, "u-social")
    assert lead["username"] == "ig_user"