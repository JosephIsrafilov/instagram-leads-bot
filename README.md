# Instagram Lead Bot — Basic Academy + Affidavit

Lead-collection chatbot for **two Instagram Professional accounts**.
No ManyChat, no paid AI service, no VPS required for development.

## Agreed behaviour

1. On a customer's first text DM, greet and ask for name + phone number.
2. Collect both (in one message or separately). A first message that already
   contains a phone number is not greeted again — the bot goes straight to the
   missing field.
3. Save them in SQLite, keyed by Instagram account + customer sender ID.
4. Send a thank-you message.
5. Stay silent afterwards; staff reply manually. After `BOT_MAX_HINTS` unanswered
   prompts the bot stops asking (status `manual`) instead of spamming.

This is **not** a generative AI chatbot: no LLM, no free-text answers about
courses, no sales. Deterministic replies only.

## Architecture

```
app.py               FastAPI: /webhook (GET verify, POST events), /health, /admin
leadsbot/config.py   env parsing + validation (lazy, no import-time side effects)
leadsbot/logic.py    pure functions: phone/name parsing, reply planning
leadsbot/storage.py  SQLite: conversations, processed_messages, outbox
leadsbot/sender.py   Instagram Graph API client (429/5xx = retryable, 4xx = permanent)
leadsbot/worker.py   outbox dispatcher, exponential backoff, lease-based claiming
leadsbot/admin.py    password-protected lead panel + CSV export
tools/dev_server.py  local run with demo accounts, console sending
tools/simulate.py    signed webhook simulator against a running server
```

Why an outbox: the reply is written in the **same transaction** as the
conversation state. A crash, restart or Instagram outage between deciding and
sending can no longer lose a message. The webhook returns 200 immediately and
the worker delivers in the background, retrying with backoff (5s, 10s, 20s …
capped at 10 min, `OUTBOX_MAX_ATTEMPTS` tries). Rejected messages land in the
`failed` list of the admin panel.

Deduplication is per `(account_id, message_id)`, so Instagram's webhook retries
never answer twice.

## Local development (no Meta app needed)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

python tools/dev_server.py         # demo accounts, IG_SEND_MODE=console, port 8000
```

In a second terminal:

```bash
python tools/simulate.py                                  # scripted conversation
python tools/simulate.py "Salam" "Aysel" "055 333 44 22"  # custom messages
python tools/simulate.py --account 17841400000000002 --sender 900000002 "Salam"
```

`simulate.py` signs payloads with `META_APP_SECRET` (defaults to `dev_secret`),
prints the bot's outbox replies and the resulting lead. In console mode the
server only logs messages instead of calling Instagram.

Admin panel: <http://127.0.0.1:8000/admin> (`admin` / `dev-admin-pass`).
CSV export: <http://127.0.0.1:8000/admin/leads.csv?status=done>.
The panel is disabled (503) when `ADMIN_PASSWORD` is empty.

Run tests:

```bash
pytest -q
```

## Production

```bash
cp .env.example .env      # fill real account ids, tokens, secret, admin password
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

- `--workers 1` is deliberate: the in-process outbox worker assumes a single
  process. Need more throughput? Run one web process plus a separate worker
  process instead of scaling web workers blindly.
- Serve behind HTTPS (Caddy or nginx) — Meta only talks to public HTTPS.
- Suggested `/etc/systemd/system/leadsbot.service`:

```ini
[Unit]
Description=Instagram leads bot
After=network.target

[Service]
WorkingDirectory=/opt/instagram_leads_bot
EnvironmentFile=/opt/instagram_leads_bot/.env
ExecStart=/opt/instagram_leads_bot/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=3
User=leadsbot

[Install]
WantedBy=multi-user.target
```

- Health check for monitoring: `GET /health` returns HTTP 503 when the database
  or configuration is broken, otherwise counters (total/done/collecting/pending/failed).
- Back up `DATABASE_PATH` (SQLite in WAL mode): `sqlite3 leads.sqlite3 ".backup backup.sqlite3"`
  on a schedule; keep backups off the server.
- Only `ADMIN_PASSWORD` holders can see phone numbers. Rotate the password if a
  manager leaves.

## Instagram / Meta setup (still to do)

1. <https://developers.facebook.com/apps/> → create an app with Instagram messaging.
2. Both accounts must be Instagram Professional (Business/Creator).
3. Permissions: `instagram_business_basic`, `instagram_business_manage_messages`.
   Confirm whether Advanced Access / App Review is required for accounts not
   owned by the app developer.
4. For each account: professional account ID + access token → `IG_ACCOUNTS_JSON`.
5. Webhook callback URL `https://<host>/webhook`, verify token = `META_VERIFY_TOKEN`,
   subscribe to the `messages` field; `META_APP_SECRET` validates POST signatures.
6. Make the first live test with an authorized account before promising a launch
   date or buying a VPS.

Docs: <https://developers.facebook.com/docs/instagram-platform/webhooks>

## Known limits / next steps

- Personal data (name, phone) is stored in plaintext SQLite. Access is limited to
  the server and the password-protected panel; add encryption-at-rest or move to
  Postgres if the client requires it.
- `processed_messages` grows forever; prune rows older than ~30 days on a cron.
- `GET /health` covers config/DB only — no alerting yet. Add uptime monitoring
  against it (and a Telegram notifier for `failed` outbox rows) before launch.
- Instagram `mid`s are assumed globally unique; dedupe is scoped per account.
- Non-text events (attachments, reactions, story replies) are ignored on purpose.