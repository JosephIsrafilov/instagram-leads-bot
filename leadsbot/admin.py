"""Password-protected lead panel and CSV export."""

from __future__ import annotations

import csv
import html
import io
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import storage
from .config import Settings, load_settings

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic(auto_error=False)

COLUMNS = [
    ("account_id", "Hesab"),
    ("sender_id", "Instagram ID"),
    ("name", "Ad"),
    ("phone", "Telefon"),
    ("status", "Status"),
    ("started_at", "Başladı"),
    ("updated_at", "Yeniləndi"),
]

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
 table{{border-collapse:collapse;width:100%;background:#1b1f29;border-radius:10px;overflow:hidden}}
 th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #262b38;vertical-align:top}}
 th{{background:#20252f;color:#9aa3b2;font-weight:600;white-space:nowrap}}
 tr:last-child td{{border-bottom:none}}
 a{{color:#6ea8fe}} code{{color:#f0b27a}}
 .err td{{color:#ff8f8f}}
</style></head><body>
<h1>Instagram lidlər</h1>
<div class="muted">Baza: <code>{db}</code> · yenilənmə: {generated}</div>
<div class="cards">
 <div class="card"><span class="muted">Ümumi söhbət</span><b>{total}</b></div>
 <div class="card"><span class="muted">Tamamlanmış lid</span><b>{done}</b></div>
 <div class="card"><span class="muted">Davam edən</span><b>{collecting}</b></div>
 <div class="card"><span class="muted">Gözləyən mesaj</span><b>{pending}</b></div>
 <div class="card"><span class="muted">Uğursuz</span><b>{failed}</b></div>
</div>
<p><a href="{csv_link}">CSV yüklə</a></p>
<h2>Söhbətlər{filter_note}</h2>
<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>
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


def _status_label(value: str) -> str:
    return {
        "new": "yeni",
        "collecting": "davam edir",
        "done": "tamamlandı",
        "manual": "menecerə",
    }.get(value, value)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(
    status_filter: str | None = Query(default=None, alias="status"),
    settings: Settings = Depends(require_admin),
) -> HTMLResponse:
    db = settings.database_path
    storage.init_db(db)
    records = storage.leads(db, status=status_filter)
    counters = storage.stats(db)
    failures = storage.failed_messages(db)

    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{_status_label(record[key]) if key == 'status' else _escape(record.get(key))}</td>"
            for key, _ in COLUMNS
        )
        + "</tr>"
        for record in records
    ) or f'<tr><td colspan="{len(COLUMNS)}" class="muted">Hələ lid yoxdur.</td></tr>'

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

    csv_link = "/admin/leads.csv" + (f"?status={status_filter}" if status_filter else "")
    return HTMLResponse(
        PAGE.format(
            db=_escape(db),
            generated=_escape(storage.now()),
            total=counters["total"],
            done=counters["done"],
            collecting=counters["collecting"],
            pending=counters["pending"],
            failed=counters["failed"],
            head="".join(f"<th>{label}</th>" for _, label in COLUMNS),
            rows=rows,
            errors=errors,
            csv_link=_escape(csv_link),
            filter_note=f" · filtr: {_escape(_status_label(status_filter))}" if status_filter else "",
        )
    )


@router.get("/leads.csv")
def export_csv(
    status_filter: str | None = Query(default=None, alias="status"),
    settings: Settings = Depends(require_admin),
) -> Response:
    storage.init_db(settings.database_path)
    records = storage.leads(settings.database_path, status=status_filter)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([key for key, _ in COLUMNS])
    for record in records:
        writer.writerow([record.get(key) or "" for key, _ in COLUMNS])
    # UTF-8 BOM keeps Azerbaijani letters readable when opened in Excel.
    body = "\ufeff" + buffer.getvalue()
    filename = "leads.csv" if not status_filter else f"leads-{status_filter}.csv"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )