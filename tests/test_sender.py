"""Instagram sender: HTTP status matrix and payload, no real network."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from leadsbot.config import load_settings
from leadsbot.sender import InstagramSender, PermanentSendError, SendError


@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "sender.sqlite3"))
    monkeypatch.setenv("IG_SEND_MODE", "api")
    monkeypatch.setenv("IG_ACCOUNTS_JSON", '{"123": {"name": "Basic Academy", "access_token": "tok"}}')
    monkeypatch.setenv("META_API_VERSION", "v26.0")
    return load_settings()


def send_with(status_code: int, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="err-body")

    sender = InstagramSender(settings, transport=httpx.MockTransport(handler))
    return asyncio.run(sender.send(settings.accounts["123"], "user-1", "Salam"))


@pytest.mark.parametrize("code", [200, 201, 204])
def test_success_codes(api_settings, code):
    assert send_with(code, api_settings) is None


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_errors_are_permanent(api_settings, code):
    with pytest.raises(PermanentSendError) as exc:
        send_with(code, api_settings)
    assert str(code) in str(exc.value)


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_server_errors_are_retryable(api_settings, code):
    with pytest.raises(SendError) as exc:
        send_with(code, api_settings)
    assert not isinstance(exc.value, PermanentSendError)


def test_timeout_is_retryable(api_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    sender = InstagramSender(api_settings, transport=httpx.MockTransport(handler))
    with pytest.raises(SendError) as exc:
        asyncio.run(sender.send(api_settings.accounts["123"], "user-1", "Salam"))
    assert "network error" in str(exc.value)


def test_request_shape(api_settings):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    sender = InstagramSender(api_settings, transport=httpx.MockTransport(handler))
    asyncio.run(sender.send(api_settings.accounts["123"], "user-42", "Salam"))
    assert seen["url"] == "https://graph.instagram.com/v26.0/123/messages"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"recipient": {"id": "user-42"}, "message": {"text": "Salam"}}


def test_console_mode_never_hits_http(api_settings, monkeypatch):
    monkeypatch.setenv("IG_SEND_MODE", "console")
    settings = load_settings()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("network must not be used in console mode")

    sender = InstagramSender(settings, transport=httpx.MockTransport(handler))
    assert asyncio.run(sender.send(settings.accounts["123"], "user-1", "Salam")) is None


# --- profile lookup (username) ------------------------------------------------
def test_fetch_profile_returns_username(api_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/900001")
        assert request.url.params["fields"] == "username,name"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"username": "@elvin_m", "name": "Elvin M"})

    sender = InstagramSender(api_settings, transport=httpx.MockTransport(handler))
    profile = asyncio.run(sender.fetch_profile(api_settings.accounts["123"], "900001"))
    assert profile == {"username": "elvin_m", "name": "Elvin M"}


def test_fetch_profile_failure_is_tolerated(api_settings):
    sender = InstagramSender(
        api_settings, transport=httpx.MockTransport(lambda _r: httpx.Response(400))
    )
    assert asyncio.run(sender.fetch_profile(api_settings.accounts["123"], "900001")) is None

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    sender = InstagramSender(api_settings, transport=httpx.MockTransport(boom))
    assert asyncio.run(sender.fetch_profile(api_settings.accounts["123"], "900001")) is None


def test_fetch_profile_skipped_in_console_mode(api_settings, monkeypatch):
    monkeypatch.setenv("IG_SEND_MODE", "console")
    settings = load_settings()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no network in console mode")

    sender = InstagramSender(settings, transport=httpx.MockTransport(handler))
    assert asyncio.run(sender.fetch_profile(settings.accounts["123"], "900001")) is None