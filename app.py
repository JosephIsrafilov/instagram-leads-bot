"""FastAPI entry point: Instagram webhook + protected lead panel.

Flow: first DM -> greeting/contact request -> collect name+phone -> thank-you -> silence.
Instagram integration uses the official Instagram Login API. No generative AI.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from leadsbot import __version__, storage
from leadsbot.admin import router as admin_router
from leadsbot.config import ConfigError, load_settings
from leadsbot.worker import OutboxWorker

load_dotenv()
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("leadsbot.app")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    storage.init_db(settings.database_path)
    worker_task: asyncio.Task[None] | None = None
    if settings.worker_enabled:
        worker_task = asyncio.create_task(OutboxWorker().run_forever(), name="outbox-worker")
    logger.info(
        "started v%s: accounts=%d db=%s send_mode=%s worker=%s",
        __version__,
        len(settings.accounts),
        settings.database_path,
        settings.send_mode,
        settings.worker_enabled,
    )
    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Instagram leads bot", version=__version__, lifespan=lifespan)
app.include_router(admin_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "instagram-leads-bot", "version": __version__}


@app.get("/health")
def health() -> JSONResponse:
    try:
        settings = load_settings()
        storage.init_db(settings.database_path)
        counters = storage.stats(settings.database_path)
    except (ConfigError, OSError) as exc:
        logger.error("health check failed: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)
    return JSONResponse({
        "status": "ok",
        "send_mode": settings.send_mode,
        "worker": settings.worker_enabled,
        "accounts": len(settings.accounts),
        **counters,
    })


def _settings_or_500():
    try:
        return load_settings()
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        raise HTTPException(status_code=500, detail="Server misconfigured") from exc


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(request: Request) -> PlainTextResponse:
    query = request.query_params
    token = _settings_or_500().verify_token
    if (
        query.get("hub.mode") != "subscribe"
        or not token
        or not hmac.compare_digest(query.get("hub.verify_token", ""), token)
    ):
        raise HTTPException(status_code=403, detail="Verification failed")
    return PlainTextResponse(query.get("hub.challenge", ""))


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@app.post("/webhook")
async def incoming(request: Request) -> dict[str, Any]:
    settings = _settings_or_500()
    if not settings.app_secret:
        raise HTTPException(status_code=503, detail="Meta app secret not configured")
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_signature(settings.app_secret, body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        data: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if data.get("object") != "instagram":
        return {"status": "ignored"}

    accepted = 0
    skipped = 0
    for entry in data.get("entry", []):
        account_id = str(entry.get("id", ""))
        if account_id not in settings.accounts:
            logger.warning("webhook for unknown account %r - ignored", account_id)
            skipped += 1
            continue
        for event in entry.get("messaging", []):
            message = event.get("message", {})
            sender = event.get("sender", {})
            text = message.get("text")
            if (
                message.get("is_echo")
                or not isinstance(text, str)
                or not sender.get("id")
                or not message.get("mid")
            ):
                skipped += 1
                continue
            try:
                plan = storage.intake(
                    settings.database_path,
                    account_id,
                    str(sender["id"]),
                    str(message["mid"]),
                    text,
                    settings.max_hints,
                )
            except Exception:
                # Instagram should still get a 200; it retries unacknowledged
                # deliveries, and the outbox holds whatever was committed.
                logger.exception("failed to process message %s", message.get("mid"))
                skipped += 1
                continue
            if plan is None:
                skipped += 1  # duplicate delivery of an already handled message
            else:
                accepted += 1
    return {"status": "accepted", "accepted": accepted, "skipped": skipped}


@app.exception_handler(ConfigError)
async def config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
    logger.error("configuration error: %s", exc)
    return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)