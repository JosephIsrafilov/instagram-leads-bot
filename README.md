# Instagram Lead Bot — Basic Academy + Affidavit

Lead-collection chatbot for **two Instagram Professional accounts**.
Deterministic: no LLM, no ManyChat, no paid AI service, no VPS needed for development.

## Agreed behaviour

1. First text DM → greeting asking for name and phone number.
2. Collect both — in one message or separately. A first message that already contains
   a phone number is not greeted again; the bot asks only for the missing field.
3. Save the lead in SQLite, keyed by Instagram account + customer sender ID.
4. Send a thank-you message.
5. Stay silent afterwards; staff take over from the panel. After `BOT_MAX_HINTS`
   unanswered prompts the bot stops asking (`manual`) instead of spamming.

Dialogue is an explicit state machine, so the bot always knows what it is waiting for:

| status | meaning |
|---|---|
| `awaiting_contact` | nothing collected yet |
| `awaiting_name` | phone known, name missing |
| `awaiting_phone` | name known, phone missing |
| `done` | lead captured, bot silent |
| `manual` | staff owns the dialogue, bot silent |

A greeting, a question or a sentence is never stored as a name: single words are
accepted as a name, multi-word candidates must look like `Aysel Məmmədova`.

## Architecture

```
app.py               FastAPI: /webhook (GET verify, POST events), /health, /admin
leadsbot/config.py   env parsing + validation (lazy, no import-time side effects)
leadsbot/logic.py    pure functions: phone/name parsing, dialogue state machine
leadsbot/storage.py  SQLite: conversations, processed_messages, outbox (+ migrations)
leadsbot/sender.py   Instagram Graph API client (429/5xx retryable, 4xx permanent)
leadsbot/notify.py   Telegram notification for staff
leadsbot/worker.py   outbox dispatcher: backoff, lease, FIFO per conversation
leadsbot/admin.py    panel: filters, pagination, manual takeover, CSV
tools/dev_server.py  local run with demo accounts, console sending
tools/simulate.py    signed webhook simulator against a running server
tools/loadtest.py    in-process load test (100 / 500 / 1000 conversations)
```

**Why an outbox.** The reply, the lead and the staff notification are written in the
**same SQLite transaction** as the conversation state, then delivered by a background
worker. A crash, redeploy or Instagram outage between "decided to answer" and "sent"
cannot lose the message. Retries use exponential backoff (5 s, 10 s, 20 s … capped at
10 min, `OUTBOX_MAX_ATTEMPTS` attempts). Rejected messages (4xx) are not retried and
show up in the panel.

**Never acknowledge what was not stored.** If persistence fails, `/webhook` answers
`503`, so Meta redelivers the event; dedupe by `(account_id, message_id)` keeps the
already-stored messages from being processed twice.

**Ordering.** Claims are FIFO per conversation: a follow-up never overtakes an
undelivered earlier reply, while a stuck conversation does not block other customers.

## Local development (no Meta app needed)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

python tools/dev_server.py         # demo accounts, IG_SEND_MODE=console, port 8000
```

Second terminal:

```bash
python tools/simulate.py                                  # scripted conversation
python tools/simulate.py "Salam" "Aysel" "055 333 44 22"  # custom messages
python tools/simulate.py --sender 901002 "0501234567" "Murad"
python tools/simulate.py --account 17841400000000002 --sender 900000002 "Salam"
```

The simulator signs payloads with `META_APP_SECRET` (defaults to `dev_secret`), prints
the replies queued by the bot and the resulting lead. In console mode the server logs
messages instead of calling Instagram.

Panel: <http://127.0.0.1:8000/admin> (`admin` / `dev-admin-pass`).
CSV: `/admin/leads.csv?status=done&account=17841400000000001`.

## Admin panel

- counters: total conversations, captured leads, in progress, in manual, queued, failed
- filters by account and status, 50 rows per page with pagination
- **Botu dayandır** — the bot stops answering that customer (`manual`) and any reply
  still queued for them is cancelled; **Bota qaytar** — hand the dialogue back and
  reset the prompts
- failed outbox messages with the exact API error
- CSV export (UTF-8 BOM, opens correctly in Excel), filterable by account/status
- disabled entirely (503) when `ADMIN_PASSWORD` is empty, HTTP Basic otherwise

## Telegram notifications (optional, recommended)

```
LEADS_TELEGRAM_TOKEN=123456:ABC...
LEADS_TELEGRAM_CHAT_IDS=420193007,123456789
```

When both are set, every captured lead is queued as a Telegram message (account, name,
phone, Instagram ID) and delivered by the same worker, so a Telegram outage retries
instead of losing the notification. Only the transition into `done` notifies: a
customer who keeps writing after the lead is captured never re-notifies managers.
Each recipient gets its own queue row, so a failure for one chat cannot duplicate the
message in another. Without the variables nothing is queued.

## Tests

```bash
pytest -q          # 95 tests
python tools/loadtest.py --count 500
```

`tests/test_qa.py` is the agreed QA checklist, one test per scenario: normal dialogue,
contact in one message, phone-first, price question, invalid number, silence after
`done`, contact refusal, two accounts with the same sender, duplicate webhook, 503 retry,
400 rejection, restart recovery, manual takeover, bad signature, database failure.
`tests/test_sender.py` covers the HTTP matrix (200/400/401/403/429/500/503/timeout)
through `httpx.MockTransport`, `tests/test_notify.py` the Telegram path.

Measured locally (in-process ASGI, no network), Windows, Python 3.12:

| conversations | webhook p50 / p95 | drain | result |
|---|---|---|---|
| 100 | 7.3 / 8.7 ms | 0.75 s | 100 separate leads, 0 failed |
| 500 | 8.2 / 9.7 ms | 3.9 s | 500 separate leads, 0 failed |
| 1000 | 8.3 / 10.7 ms | 8.9 s | 1000 separate leads, 0 failed |

## Production

```bash
cp .env.example .env      # real account ids, tokens, secret, admin password, Telegram
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

- `--workers 1` is deliberate: the in-process outbox worker assumes one process. For
  more throughput run the web process and a separate worker process.
- Serve behind HTTPS (Caddy/nginx) — Meta only talks to public HTTPS.
- systemd unit:

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

- `GET /health` returns 503 when config or the database is broken, otherwise counters.
- Backup: `sqlite3 leads.sqlite3 ".backup backup.sqlite3"` on a schedule, off-server.
- Only `ADMIN_PASSWORD` holders see phone numbers; rotate when staff leave.

## Instagram / Meta setup (still to do)

1. <https://developers.facebook.com/apps/> → app with Instagram messaging.
2. Both accounts must be Instagram Professional (Business/Creator).
3. Permissions: `instagram_business_basic`, `instagram_business_manage_messages`.
   Advanced Access / App Review may be required for accounts the app does not own.
4. For each account: professional account ID + access token → `IG_ACCOUNTS_JSON`.
5. Webhook `https://<host>/webhook`, verify token = `META_VERIFY_TOKEN`, subscribe to
   `messages`; `META_APP_SECRET` validates POST signatures.
6. Test with an account added as Instagram Tester before promising a launch date.

Docs: <https://developers.facebook.com/docs/instagram-platform/webhooks>

## Known limits

- Personal data (name, phone) is stored in plaintext SQLite; access is limited to the
  server and the password-protected panel. Encrypt at rest or move to Postgres if the
  client requires it.
- `processed_messages` grows forever — prune rows older than ~30 days on a cron.
- No alerting yet: add uptime monitoring on `/health` plus a notifier for `failed`
  outbox rows.
- Absolute exactly-once delivery is impossible with any HTTP API: a request may reach
  Instagram while its response is lost, and the retry would duplicate the DM.
- Attachments, reactions and story replies are ignored on purpose.
- Real Instagram (permissions, live webhooks, Direct delivery) is not verified yet —
  that is the next stage, and it needs Meta access.
