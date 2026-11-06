"""Admin panel: lead queue, search, lead detail with message history, CSV export.

Surface: **Operate** (a manager works a queue and takes actions), so the layout is
a dense list with one primary action per row - no hero, no cards-for-decoration.
"""

from __future__ import annotations

import csv
import html
import io
import secrets
from datetime import datetime, timezone
from string import Template
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import storage
from .config import Settings, load_settings

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic(auto_error=False)

PAGE_SIZE = 40
ACTIONABLE = {"manual", "awaiting_contact", "done"}

STATUS_LABELS = {
    "awaiting_contact": "Əlaqə gözlənilir",
    "awaiting_name": "Ad gözlənilir",
    "awaiting_phone": "Nömrə gözlənilir",
    "done": "Lid hazır",
    "manual": "Menecerdə",
}
STATUS_TONE = {
    "awaiting_contact": "wait",
    "awaiting_name": "wait",
    "awaiting_phone": "wait",
    "done": "ok",
    "manual": "info",
}
GROUP_FILTERS = {
    "waiting": "('awaiting_contact','awaiting_name','awaiting_phone')",
}

BASE_CSS = """
:root{
  --bg:#0e1116; --surface:#151922; --surface-2:#1b202b; --line:#242b39;
  --ink:#e8ecf4; --muted:#a4aebe; --muted-2:#7d8798;
  --accent:#5b8def; --accent-ink:#0b1220;
  --ok:#4ade80; --wait:#fbbf24; --bad:#f87171; --muted-tone:#94a3b8;
  --radius:10px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
body{font:14px/1.5 ui-sans-serif,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code,.num{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 64px}

/* top bar */
.bar{position:sticky;top:0;z-index:5;background:rgba(14,17,22,.92);backdrop-filter:blur(6px);
  border-bottom:1px solid var(--line)}
.bar-in{max-width:1180px;margin:0 auto;padding:14px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.brand{font-weight:650;font-size:15px;letter-spacing:.01em;display:flex;gap:8px;align-items:center}
.brand small{color:var(--muted);font-weight:400}
.search{flex:1 1 260px;display:flex;min-width:220px}
.search input{width:100%;background:var(--surface);border:1px solid var(--line);border-right:none;
  border-radius:8px 0 0 8px;color:var(--ink);padding:8px 12px;font:inherit}
.search input::placeholder{color:var(--muted-2)}
.search button{background:var(--surface-2);border:1px solid var(--line);border-left:none;
  border-radius:0 8px 8px 0;color:var(--ink);padding:0 14px;cursor:pointer;font:inherit}
.search button:hover{background:#222836}
.btn{display:inline-flex;align-items:center;gap:6px;background:var(--surface-2);border:1px solid var(--line);
  color:var(--ink);padding:7px 12px;border-radius:8px;font:inherit;cursor:pointer;min-height:34px}
.btn:hover{background:#222836;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn.primary:hover{filter:brightness(1.06)}
.btn.small{min-height:28px;padding:4px 9px;font-size:12.5px}

/* tabs + stats */
.tabs{display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin:14px 0 0;border-bottom:1px solid var(--line)}
.tabs a{padding:8px 12px;color:var(--muted);border-bottom:2px solid transparent;font-size:13.5px}
.tabs a:hover{color:var(--ink);text-decoration:none}
.tabs a.on{color:var(--ink);border-color:var(--accent)}
.stats{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin:12px 0 18px}
.stats b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.stats .bad b{color:var(--bad)}
.stats .warn b{color:var(--wait)}

/* list */
.list{border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;background:var(--surface)}
.head,.row{display:grid;grid-template-columns:minmax(180px,1.6fr) 150px 130px 150px 110px 168px;
  gap:12px;align-items:center;padding:11px 14px}
.head{background:var(--surface-2);color:var(--muted);font-size:12px;text-transform:uppercase;
  letter-spacing:.06em;border-bottom:1px solid var(--line)}
.row{border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:none}
.row:hover{background:#171c26}
.who{min-width:0}
.who .name{font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who .handle{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who .handle a{color:var(--muted)}
.phone{color:var(--ink)}
.phone.empty,.handle.empty{color:var(--muted-2)}
.account{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.age{color:var(--muted);font-size:12.5px}
.actions{display:flex;gap:6px;justify-content:flex-end}

.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;
  font-size:12.5px;background:var(--surface-2);border:1px solid var(--line);white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted-tone)}
.pill.ok .dot{background:var(--ok)} .pill.ok{color:#bbf7d0;border-color:#21402f}
.pill.wait .dot{background:var(--wait)} .pill.wait{color:#fde68a;border-color:#443a17}
.pill.muted .dot{background:var(--muted-tone)}
.pill.info .dot{background:var(--accent)} .pill.info{color:#c7d7fb;border-color:#28405f}
.pill.bad .dot{background:var(--bad)} .pill.bad{color:#fecaca;border-color:#4a2323}

.filter-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.group-label{color:var(--muted-2);font-size:11px;text-transform:uppercase;letter-spacing:.08em;min-width:48px}
.stats a{color:var(--muted);padding:2px 0}
.stats a:hover{color:var(--ink);text-decoration:none}
.stats a b{color:var(--ink)}
.stats .zero{opacity:.5}
*::-webkit-scrollbar{width:12px;height:12px}
*::-webkit-scrollbar-track{background:var(--bg)}
*::-webkit-scrollbar-thumb{background:#2a3140;border-radius:8px;border:3px solid var(--bg)}
*::-webkit-scrollbar-thumb:hover{background:#374055}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.who .name a{color:var(--ink)}
.who .name a:hover{text-decoration:underline}

.empty{padding:48px 20px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--ink);margin-bottom:6px;font-size:15px}

.pager{display:flex;gap:10px;align-items:center;margin-top:16px;color:var(--muted);font-size:13px}
.pager .spacer{flex:1}

/* detail */
.crumbs{margin:18px 0 10px;color:var(--muted);font-size:13px}
.detail{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:18px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px}
.card h2{margin:0 0 14px;font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
.kv{display:grid;grid-template-columns:110px 1fr;gap:10px 14px;align-items:baseline}
.kv dt{color:var(--muted);font-size:13px}
.kv dd{margin:0;font-size:15px}
.kv dd.big{font-size:19px;font-weight:600;letter-spacing:.01em}
.detail .actions{margin-top:16px;justify-content:flex-start;flex-wrap:wrap}

/* timeline */
.feed{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;max-height:520px;overflow:auto}
.feed li{background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.feed .meta{display:flex;gap:10px;color:#b6c0d2;font-size:12px;margin-bottom:5px;
  text-transform:uppercase;letter-spacing:.05em;align-items:center}
.feed .meta .state{color:var(--muted-2)}
.feed .meta .when{margin-left:auto;text-transform:none;letter-spacing:0}
.feed .body{white-space:pre-wrap;font-size:13.5px}
.feed li.bad{border-color:#4a2323}
.feed li.in{background:#131a26;border-color:#2b3448}
.feed li.in .meta span:first-child{color:#9fc0ff}
.feed li.bad .body{color:#fecaca}
.feed li.waiting .meta{color:var(--wait)}

.note{color:var(--muted);font-size:12.5px;margin-top:10px}
.errs{margin-top:34px}
.errs table{width:100%;border-collapse:collapse;font-size:13px}
.errs th,.errs td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.errs th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}

@media (max-width:900px){
  .head{display:none}
  .row{grid-template-columns:1fr;gap:8px;padding:14px}
  .row .actions{justify-content:flex-start}
  .detail{grid-template-columns:1fr}
  .stats{gap:12px}
}
"""

LIST_PAGE = Template("""<!doctype html>
<html lang="az"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lidlər · $count</title>
<style>$css</style></head><body>
<div class="bar"><div class="bar-in">
  <div class="brand" title="$db_name">Lidlər <small>· $env_label</small></div>
  <form class="search" method="get" action="/admin">
    $hidden_filters
    <input name="q" value="$q" placeholder="Ad, telefon, @username və ya ID…" autocomplete="off">
    <button type="submit">Axtar</button>
  </form>
  <a class="btn" href="$csv_link">CSV</a>
</div></div>

<div class="wrap">
  <div class="tabs"><span class="group-label">Hesab</span>$account_tabs</div>
  <div class="tabs"><span class="group-label">Status</span>$status_tabs</div>
  <div class="stats">$stats</div>

  <div class="list">
    <div class="head">
      <div>Müştəri</div><div>Telefon</div><div>Hesab</div><div>Status</div><div>Yeniləndi</div>
      <div style="text-align:right">Əməliyyat</div>
    </div>
    $rows
  </div>
  <div class="pager">
    <span>$range_text</span><span class="spacer"></span>$pager
  </div>

  <div class="errs">
    <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600">
      Göndərilə bilməyən mesajlar$error_count</h2>
    $errors
  </div>
</div>
</body></html>""")

DETAIL_PAGE = Template("""<!doctype html>
<html lang="az"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title · lid</title>
<style>$css</style></head><body>
<div class="bar"><div class="bar-in">
  <div class="brand">Lidlər <small>· $account_name</small></div>
  <a class="btn" href="$back">← Siyahıya</a>
</div></div>
<div class="wrap">
  <div class="crumbs">$account_name · Instagram ID <code>$sender_id</code></div>
  <div class="detail">
    <div class="card">
      <h2>Müştəri</h2>
      <dl class="kv">
        <dt>Ad</dt><dd class="big">$name</dd>
        <dt>Telefon</dt><dd class="big">$phone_html</dd>
        <dt>Instagram</dt><dd>$username_html</dd>
        <dt>Status</dt><dd>$status_pill</dd>
        <dt>Başladı</dt><dd class="age">$started</dd>
        <dt>Yeniləndi</dt><dd class="age">$updated</dd>
      </dl>
      <div class="actions">
        <form method="post" action="$action_url">
          <input type="hidden" name="new_status" value="$next_status">
          <input type="hidden" name="back" value="$back">
          <button class="btn primary">$action_label</button>
        </form>
      </div>
      <p class="note">Telefon və ad — şəxsi məlumatdır. Yalnız sifarişlə əlaqə üçün istifadə edin.</p>
    </div>
    <div class="card">
      <h2>Söhbət</h2>
      $feed
    </div>
  </div>
</div>
</body></html>""")


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
    settings: Settings = Depends(load_settings),
) -> Settings:
    if not settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_PASSWORD is not configured; admin panel disabled",
        )
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, settings.admin_user)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return settings


# --- small formatting helpers -------------------------------------------------
def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _qs(**params: Any) -> str:
    parts = [f"{key}={html.escape(str(value))}" for key, value in params.items() if value]
    return "&".join(parts)


def _pill(status_value: str | None) -> str:
    tone = STATUS_TONE.get(status_value or "", "muted")
    return (
        f'<span class="pill {tone}"><span class="dot"></span>'
        f"{_e(STATUS_LABELS.get(status_value or '', status_value))}</span>"
    )


def _age(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return _e(iso[:16])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - moment).total_seconds()
    if delta < 60:
        return "indi"
    if delta < 3600:
        return f"{int(delta // 60)} dəq əvvəl"
    if delta < 86400:
        return f"{int(delta // 3600)} saat əvvəl"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} gün əvvəl"
    return moment.strftime("%d.%m.%Y")


def _when(iso: str | None) -> str:
    """Absolute local timestamp for the message feed."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%d.%m %H:%M")


def _phone_cell(phone: str | None) -> str:
    if not phone:
        return '<span class="phone empty">—</span>'
    tel = _e(phone.replace(" ", ""))
    return (
        f'<a class="phone num" href="tel:{tel}" title="{_e(phone)}">'
        f"{_e(_fmt_phone(phone))}</a>"
    )


def _fmt_phone(phone: str) -> str:
    """+994501234567 -> +994 50 123 45 67 for scanning."""
    digits = phone[1:] if phone.startswith("+") else phone
    if phone.startswith("+994") and len(digits) == 12:
        return f"+994 {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:12]}"
    return phone


def _stats_block(
    counters: dict[str, Any], account: str | None, status_filter: str | None, q: str | None
) -> str:
    parts = []
    for label, key, target in (
        ("söhbət", "total", None),
        ("lid hazır", "done", "done"),
        ("davam edən", "waiting", "waiting"),
        ("menecerdə", "manual", "manual"),
    ):
        value = counters[key]
        css = "zero" if value == 0 else ""
        parts.append(
            f'<a class="{css}" href="/admin?{_qs(account=account, status=target, q=q)}">'
            f"<b>{value}</b> {_e(label)}</a>"
        )
    for label, key, tone in (
        ("növbədə", "pending", "warn"),
        ("uğursuz", "failed", "bad"),
        ("ləğv", "cancelled", ""),
    ):
        value = counters[key]
        css = "zero" if value == 0 else tone
        parts.append(f'<span class="{css}"><b>{value}</b> {_e(label)}</span>')
    return "".join(parts)


def _username_cell(username: str | None) -> str:
    if not username:
        return '<span class="handle empty">—</span>'
    return (
        f'<a href="https://instagram.com/{_e(username)}" target="_blank" rel="noopener">'
        f"@{_e(username)}</a>"
    )


def _lead_url(account_id: str, sender_id: str) -> str:
    return f"/admin/lead/{html.escape(account_id)}/{html.escape(sender_id)}"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(
    status_filter: str | None = Query(default=None, alias="status"),
    account: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    settings: Settings = Depends(require_admin),
) -> HTMLResponse:
    db = settings.database_path
    storage.init_db(db)
    needle = (q or "").strip() or None
    records = storage.leads(
        db,
        status=status_filter,
        account_id=account,
        q=needle,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    filtered_total = storage.count_leads(db, status=status_filter, account_id=account, q=needle)
    counters = storage.stats(db)
    failures = storage.failed_messages(db, limit=10)

    rows = []
    for record in records:
        username = record.get("username")
        name = record.get("name") or "Adsız müştəri"
        if username:
            subtitle = (
                f'<a href="https://instagram.com/{_e(username)}" target="_blank" '
                f'rel="noopener">@{_e(username)}</a>'
            )
        else:
            subtitle = f"ID {_e(record['sender_id'])}"
        stop_target = "awaiting_contact" if record["status"] == "manual" else "manual"
        stop_label = "Bota qaytar" if record["status"] == "manual" else "Dayandır"
        rows.append(
            '<div class="row">'
            f'<div class="who"><div class="name"><a href="{_lead_url(record["account_id"], record["sender_id"])}" title="{_e(name)}">'
            f"{_e(name)}</a></div>"
            f'<div class="handle">{subtitle}</div></div>'
            f"<div>{_phone_cell(record.get('phone'))}</div>"
            f'<div class="account" title="{_e(_account_name(settings, record["account_id"]))}">'
            f"{_e(_account_name(settings, record['account_id']))}</div>"
            f"<div>{_pill(record['status'])}</div>"
            f'<div class="age">{_age(record.get("updated_at"))}</div>'
            '<div class="actions">'
            f"<form method='post' action='{_e(record['account_id'])}/{_e(record['sender_id'])}/status'>"
            f"<input type='hidden' name='new_status' value='{stop_target}'>"
            f"<input type='hidden' name='back' value='{_e(_back_url(account, status_filter, needle, page))}'>"
            f"<button class='btn small'>{stop_label}</button></form>"
            f"<a class='btn small' href='{_lead_url(record['account_id'], record['sender_id'])}'>→</a>"
            "</div></div>"
        )
    if not rows:
        hint = "Axtarışa uyğun heç nə tapılmadı." if needle else "Hələ heç bir müraciət yoxdur."
        action = (
            '<p class="note">Lokal test üçün: <code>python tools/simulate.py --sender 900001 '
            '"Salam" "Elvin" "050 123 45 67"</code></p>'
            if not needle
            else '<p class="note"><a href="/admin">Filtri sıfırla</a></p>'
        )
        rows.append(f'<div class="empty"><b>{hint}</b>{action}</div>')

    pages = max(1, -(-filtered_total // PAGE_SIZE))
    pager_parts = []
    if page > 1:
        pager_parts.append(f'<a class="btn small" href="{_back_url(account, status_filter, needle, page - 1)}">← Əvvəlki</a>')
    pager_parts.append(f"<span>{page} / {pages}</span>")
    if page < pages:
        pager_parts.append(f'<a class="btn small" href="{_back_url(account, status_filter, needle, page + 1)}">Növbəti →</a>')

    shown_from = (page - 1) * PAGE_SIZE + 1 if filtered_total else 0
    shown_to = shown_from + len(records) - 1 if records else 0

    account_tabs = _tabs(
        [("Hamısı", _qs(status=status_filter, q=needle), account is None)]
        + [
            (
                name,
                _qs(account=acc_id, status=status_filter, q=needle),
                account == acc_id,
            )
            for acc_id, name in _account_options(settings)
        ]
    )
    status_tabs = _tabs(
        [
            ("Hamısı", _qs(account=account, q=needle), status_filter is None),
            ("Lid hazır", _qs(account=account, status="done", q=needle), status_filter == "done"),
            ("Davam edən", _qs(account=account, status="waiting", q=needle), status_filter == "waiting"),
            ("Menecerdə", _qs(account=account, status="manual", q=needle), status_filter == "manual"),
        ]
    )

    errors = _errors_block(failures)
    return HTMLResponse(
        LIST_PAGE.substitute(
            css=BASE_CSS,
            count=filtered_total,
            db_name=_e(db),
            env_label="canlı" if settings.send_mode == "api" else "lokal",
            q=_e(needle or ""),
            hidden_filters=(
                (f'<input type="hidden" name="account" value="{_e(account)}">' if account else "")
                + (f'<input type="hidden" name="status" value="{_e(status_filter)}">' if status_filter else "")
            ),
            csv_link=_e("/admin/leads.csv?" + _qs(status=status_filter, account=account, q=needle)),
            account_tabs=account_tabs,
            status_tabs=status_tabs,
            stats=_stats_block(counters, account, status_filter, needle),
            rows="".join(rows),
            range_text=f"{shown_from}–{shown_to} / {filtered_total}" if filtered_total else "0 / 0",
            pager=" ".join(pager_parts),
            error_count=f" ({len(failures)})" if failures else "",
            errors=errors,
        )
    )


@router.get("/lead/{account_id}/{sender_id}", response_class=HTMLResponse)
def lead_detail(
    account_id: str,
    sender_id: str,
    settings: Settings = Depends(require_admin),
) -> HTMLResponse:
    db = settings.database_path
    lead = storage.get_lead(db, account_id, sender_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    timeline = storage.lead_timeline(db, account_id, sender_id)

    items = []
    for entry in timeline:
        inbound = entry["direction"] == "in"
        when = _e(_when(entry["at"]))
        if inbound:
            tone, label, state = "in", "Müştəri", ""
        else:
            state_key = entry["state"]
            tone = {"failed": "bad", "cancelled": "bad", "queued": "waiting"}.get(state_key, "")
            label = "Telegram" if entry["channel"] == "telegram" else "Instagram"
            state = {
                "sent": "göndərildi",
                "failed": "uğursuz",
                "cancelled": "ləğv",
                "queued": "növbədə",
            }.get(state_key, state_key)
        items.append(
            f'<li class="{tone}"><div class="meta"><span>{label}</span>'
            f'<span class="state">{state}</span>'
            f'<span class="when" title="{_e(entry["at"])}">{when}</span></div>'
            f'<div class="body">{_e(entry["body"])}</div></li>'
        )
    feed = (
        f'<ul class="feed">{"".join(items)}</ul>'
        if items
        else '<p class="note">Bu müştəri ilə hələ mesaj mübadiləsi yoxdur.</p>'
    )

    is_manual = lead["status"] == "manual"
    return HTMLResponse(
        DETAIL_PAGE.substitute(
            css=BASE_CSS,
            title=_e(lead.get("name") or lead["sender_id"]),
            account_name=_e(_account_name(settings, account_id)),
            sender_id=_e(sender_id),
            name=_e(lead.get("name") or "—"),
            phone_html=_phone_cell(lead.get("phone")),
            username_html=_username_cell(lead.get("username")),
            status_pill=_pill(lead["status"]),
            started=_age(lead.get("started_at")),
            updated=_age(lead.get("updated_at")),
            back=_e(_back_url(None, None, None, 1)),
            action_url=f"/admin/{html.escape(account_id)}/{html.escape(sender_id)}/status",
            next_status="awaiting_contact" if is_manual else "manual",
            action_label="Bota qaytar" if is_manual else "Botu dayandır",
            feed=feed,
        )
    )


def _account_options(settings: Settings) -> list[tuple[str, str]]:
    return [(account.account_id, account.name) for account in settings.accounts.values()]


def _account_name(settings: Settings, account_id: str) -> str:
    account = settings.accounts.get(account_id)
    return account.name if account else account_id


def _tabs(items: list[tuple[str, str, bool]]) -> str:
    parts = []
    for label, query, active in items:
        css = ' class="on"' if active else ""
        parts.append(f'<a href="/admin?{query}"{css}>{_e(label)}</a>')
    return "".join(parts)


def _back_url(
    account: str | None, status_filter: str | None, q: str | None, page: int
) -> str:
    query = _qs(
        account=account,
        status=status_filter,
        q=q,
        page=page if page > 1 else None,
    )
    return "/admin" + (f"?{query}" if query else "")


def _errors_block(failures: list[dict[str, Any]]) -> str:
    if not failures:
        return '<p class="note">Uğursuz mesaj yoxdur.</p>'
    rows = "".join(
        "<tr>"
        f"<td class='num'>{row['id']}</td>"
        f"<td>{_e(row['account_id'])}<br><span class='note'>{_e(row['sender_id'])}</span></td>"
        f"<td>{_e(row['body'])[:160]}</td>"
        f"<td class='num'>{row['attempts']}</td>"
        f"<td class='note'>{_e(row['last_error'])[:160]}</td>"
        "</tr>"
        for row in failures
    )
    return (
        "<table><thead><tr><th>#</th><th>Hesab / alıcı</th><th>Mətn</th>"
        f"<th>Cəhd</th><th>Xəta</th></tr></thead><tbody>{rows}</tbody></table>"
    )


@router.post("/{account_id}/{sender_id}/status")
def change_status(
    account_id: str,
    sender_id: str,
    new_status: str = Form(),
    back: str = Form(default="/admin"),
    settings: Settings = Depends(require_admin),
) -> RedirectResponse:
    if new_status not in ACTIONABLE:
        raise HTTPException(status_code=400, detail="Unknown status")
    if not storage.set_status(settings.database_path, account_id, sender_id, new_status):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if new_status == "manual":
        # Staff own the dialogue now: drop anything still queued so the bot cannot
        # answer over the manager's head.
        storage.cancel_pending(
            settings.database_path, account_id, sender_id, "cancelled: staff stopped the bot"
        )
    target = back if back.startswith("/admin") else "/admin"
    return RedirectResponse(url=target, status_code=303)


@router.get("/leads.csv")
def export_csv(
    status_filter: str | None = Query(default=None, alias="status"),
    account: str | None = Query(default=None),
    q: str | None = Query(default=None),
    settings: Settings = Depends(require_admin),
) -> Response:
    storage.init_db(settings.database_path)
    records = storage.leads(
        settings.database_path,
        status=status_filter,
        account_id=account,
        q=(q or "").strip() or None,
        limit=100_000,
    )
    columns = [
        ("account_id", "account"),
        ("sender_id", "instagram_id"),
        ("username", "username"),
        ("name", "name"),
        ("phone", "phone"),
        ("status", "status"),
        ("started_at", "started_at"),
        ("updated_at", "updated_at"),
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([heading for _, heading in columns])
    for record in records:
        writer.writerow([record.get(key) or "" for key, _ in columns])
    # UTF-8 BOM keeps Azerbaijani letters readable when opened in Excel.
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )