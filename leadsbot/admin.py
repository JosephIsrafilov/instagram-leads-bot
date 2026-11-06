"""Password-protected lead panel: filters, pagination, manual takeover, CSV."""

from __future__ import annotations

import csv
import html
import io
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import storage
from .config import Settings, load_settings

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic(auto_error=False)

PAGE_SIZE = 50
ACTIONABLE = {"manual", "awaiting_contact", "done"}

COLUMNS = [
    ("account_id", "Hesab"),
    ("sender_id", "Instagram ID"),
    ("name", "Ad"),
    ("phone", "Telefon"),
    ("status", "Status"),
    ("updated_at", "Yeniləndi"),
]

STATUS_LABELS = {
    "awaiting_contact": "əlaqə gözlənilir",
    "awaiting_name": "ad gözlənilir",
    "awaiting_phone": "nömrə gözlənilir",
    "done": "tamamlandı",
    "manual": "menecer",
}

PAGE = """<!doctype html>
<html lang="az"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lidlər</title>
<style>
 body{{background:#12141a;color:#e6e8ee;font:14px/1.5 system-ui,Segoe UI,Arial,sans-serif;margin:0;padding:24px}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px;color:#9aa3b2}}
 .muted{{color:#9aa3b2}}
 .cards{{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}}
 .card{{background:#1b1f29;border:1px solid #262b38;border-radius:10px;padding:10px 14px;min-width:110px}}
 .card b{{display:block;font-size:20px}}
 .filters{{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}}
 .filters a{{background:#1b1f29;border:1px solid #262b38;border-radius:999px;padding:3px 10px;
   color:#c7ccd8;text-decoration:none;font-size:13px}}
 .filters a.on{{background:#2a3550;border-color:#3d4f7a;color:#e6e8ee}}
 table{{border-collapse:collapse;width:100%;background:#1b1f29;border-radius:10px;overflow:hidden}}
 th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #262b38;vertical-align:top}}
 th{{background:#20252f;color:#9aa3b2;font-weight:600;white-space:nowrap}}
 tr:last-child td{{border-bottom:none}}
 a{{color:#6ea8fe}} code{{color:#f0b27a}}
 button{{background:#2a3550;border:1px solid #3d4f7a;color:#dbe3f5;border-radius:6px;
   padding:3px 8px;font-size:12px;cursor:pointer}}
 .err td{{color:#ff8f8f}}
</style></head><body>
<h1>Instagram lidlər</h1>
<div class="muted">Baza: <code>{db}</code> · yenilənmə: {generated}</div>
<div class="cards">
 <div class="card"><span class="muted">Ümumi söhbət</span><b>{total}</b></div>
 <div class="card"><span class="muted">Tamamlanmış lid</span><b>{done}</b></div>
 <div class="card"><span class="muted">Davam edən</span><b>{waiting}</b></div>
 <div class="card"><span class="muted">Menecerdə</span><b>{manual}</b></div>
 <div class="card"><span class="muted">Gözləyən mesaj</span><b>{pending}</b></div>
 <div class="card"><span class="muted">Uğursuz</span><b>{failed}</b></div>
 <div class="card"><span class="muted">Ləğv edilmiş</span><b>{cancelled}</b></div>
</div>
<p><a href="{csv_link}">CSV yüklə</a></p>
<div class="filters">Hesab: {account_filters}</div>
<div class="filters">Status: {status_filters}</div>
<h2>Söhbətlər <span class="muted">({shown} / {filtered_total})</span></h2>
<table><thead><tr>{head}<th></th></tr></thead><tbody>{rows}</tbody></table>
<p class="muted">{pager}</p>
<h2>Göndərilə bilməyən mesajlar</h2>
{errors}
</body></html>"""


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


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "")


def _filters(settings: Settings, account: str | None, chosen: str | None) -> str:
    def link(label: str, **params: Any) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v)
        css = " class='on'" if params.get("account") == account and params.get("status") == chosen else ""
        return f"<a href='/admin?{query}'{css}>{_escape(label)}</a>"

    return link("hamısı", status=chosen) + "".join(
        link(name, account=account_id, status=chosen)
        for account_id, name in [(a.account_id, a.name) for a in settings.accounts.values()]
    )


def _status_filters(account: str | None, chosen: str | None) -> str:
    def link(label: str, value: str | None) -> str:
        query = "&".join(f"{k}={v}" for k, v in {"account": account, "status": value}.items() if v)
        css = " class='on'" if value == chosen else ""
        return f"<a href='/admin?{query}'{css}>{_escape(label)}</a>"

    return (
        link("hamısı", None)
        + link("lid olanlar", "done")
        + link("davam edən", "awaiting_contact")
        + link("menecerdə", "manual")
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(
    status_filter: str | None = Query(default=None, alias="status"),
    account: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    settings: Settings = Depends(require_admin),
) -> HTMLResponse:
    db = settings.database_path
    storage.init_db(db)
    records = storage.leads(
        db, status=status_filter, account_id=account, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    filtered_total = storage.count_leads(db, status=status_filter, account_id=account)
    counters = storage.stats(db)
    failures = storage.failed_messages(db)

    rows = []
    for record in records:
        cells = "".join(
            f"<td>{_label(record.get(key)) if key == 'status' else _escape(record.get(key))}</td>"
            for key, _ in COLUMNS
        )
        target = "awaiting_contact" if record["status"] == "manual" else "manual"
        button = "Bota qaytar" if record["status"] == "manual" else "Botu dayandır"
        action = (
            "<td><form method='post'"
            f" action='/admin/{_escape(record['account_id'])}/{_escape(record['sender_id'])}/status'>"
            f"<input type='hidden' name='new_status' value='{target}'>"
            f"<input type='hidden' name='back' value='{_escape(_back_url(account, status_filter, page))}'>"
            f"<button>{button}</button></form></td>"
        )
        rows.append(f"<tr>{cells}{action}</tr>")
    if not rows:
        rows.append(f'<tr><td colspan="{len(COLUMNS) + 1}" class="muted">Hələ lid yoxdur.</td></tr>')

    errors = (
        "<table class=\"err\"><thead><tr><th>#</th><th>Hesab</th><th>Alıcı</th>"
        "<th>Mətn</th><th>Cəhd</th><th>Xəta</th></tr></thead><tbody>"
        + "".join(
            "<tr>"
            f"<td>{row['id']}</td><td>{_escape(row['account_id'])}</td>"
            f"<td>{_escape(row['sender_id'])}</td><td>{_escape(row['body'])}</td>"
            f"<td>{row['attempts']}</td><td>{_escape(row['last_error'])}</td>"
            "</tr>"
            for row in failures
        )
        + "</tbody></table>"
        if failures
        else '<p class="muted">Uğursuz mesaj yoxdur.</p>'
    )

    pages = max(1, -(-filtered_total // PAGE_SIZE))
    pager = ""
    if pages > 1:
        parts = []
        if page > 1:
            parts.append(f"<a href='{_escape(_back_url(account, status_filter, page - 1))}'>← əvvəlki</a>")
        parts.append(f"səhifə {page} / {pages}")
        if page < pages:
            parts.append(f"<a href='{_escape(_back_url(account, status_filter, page + 1))}'>növbəti →</a>")
        pager = " · ".join(parts)

    csv_link = "/admin/leads.csv?" + "&".join(
        f"{k}={v}" for k, v in {"status": status_filter, "account": account}.items() if v
    )
    return HTMLResponse(
        PAGE.format(
            db=_escape(db),
            generated=_escape(storage.now()),
            total=counters["total"],
            done=counters["done"],
            waiting=counters["waiting"],
            manual=counters["manual"],
            pending=counters["pending"],
            failed=counters["failed"],
            cancelled=counters["cancelled"],
            head="".join(f"<th>{label}</th>" for _, label in COLUMNS),
            rows="".join(rows),
            errors=errors,
            csv_link=_escape(csv_link),
            account_filters=_filters(settings, account, status_filter),
            status_filters=_status_filters(account, status_filter),
            shown=len(records),
            filtered_total=filtered_total,
            pager=pager,
        )
    )


def _back_url(account: str | None, status_filter: str | None, page: int) -> str:
    params = {"account": account, "status": status_filter, "page": page if page > 1 else None}
    query = "&".join(f"{k}={v}" for k, v in params.items() if v)
    return "/admin" + (f"?{query}" if query else "")


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
    settings: Settings = Depends(require_admin),
) -> Response:
    storage.init_db(settings.database_path)
    records = storage.leads(
        settings.database_path, status=status_filter, account_id=account, limit=100_000
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([key for key, _ in COLUMNS] + ["started_at"])
    for record in records:
        writer.writerow([record.get(key) or "" for key, _ in COLUMNS] + [record.get("started_at") or ""])
    # UTF-8 BOM keeps Azerbaijani letters readable when opened in Excel.
    body = "\ufeff" + buffer.getvalue()
    filename = "leads.csv"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )