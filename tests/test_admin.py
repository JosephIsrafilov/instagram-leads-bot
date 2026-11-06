"""Admin panel: auth, HTML rendering, CSV export."""

from __future__ import annotations

import csv
import io

from conftest import ACCOUNT_A

AUTH = ("admin", "test-admin-pass")


def seed(settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "user-1", "m-1", "Salam")
    storage.intake(settings.database_path, ACCOUNT_A, "user-1", "m-2", "Elvin 055 333 44 22")


def test_panel_requires_credentials(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong-pass")).status_code == 401
    assert client.get("/admin", auth=AUTH).status_code == 200


def test_panel_lists_lead_and_escapes_html(client, settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "<script>x</script>", "m-3", "Salam")
    response = client.get("/admin", auth=AUTH)
    assert response.status_code == 200
    assert "&lt;script&gt;x&lt;/script&gt;" in response.text
    assert "<script>x</script>" not in response.text


def test_panel_is_disabled_without_password(client, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD")
    assert client.get("/admin", auth=AUTH).status_code == 503


def test_csv_export(client, settings):
    seed(settings)
    response = client.get("/admin/leads.csv", auth=AUTH)
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1
    assert rows[0]["name"] == "Elvin"
    assert rows[0]["phone"].endswith("4422")
    assert rows[0]["status"] == "done"
    assert rows[0]["account_id"] == ACCOUNT_A


def test_csv_status_filter(client, settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "user-2", "m-4", "Salam")
    rows = client.get("/admin/leads.csv?status=awaiting_contact", auth=AUTH)
    text = rows.content.decode("utf-8-sig")
    assert "user-2" in text
    assert "user-1" not in text


def test_health_reports_counts(client, settings):
    seed(settings)
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["total"] == 1
    assert payload["done"] == 1
    assert payload["pending"] == 2