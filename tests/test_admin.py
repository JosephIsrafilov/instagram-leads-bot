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
    assert rows[0]["account"] == ACCOUNT_A


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


# --- username, search, lead detail -------------------------------------------
def test_panel_shows_username_and_profile_link(client, settings):
    from leadsbot import storage

    storage.intake(
        settings.database_path, ACCOUNT_A, "user-9", "m-9", "Elvin 0501234567", username="elvin_m"
    )
    page = client.get("/admin", auth=AUTH).text
    assert "@elvin_m" in page
    assert "https://instagram.com/elvin_m" in page


def test_search_matches_username_name_and_phone(client, settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "user-a", "m-a", "Elvin 0501234567", username="elvin_m")
    storage.intake(settings.database_path, ACCOUNT_A, "user-b", "m-b", "Aysel 0553334422", username="aysel_g")

    by_username = client.get("/admin?q=elvin_m", auth=AUTH).text
    assert "Elvin" in by_username and "Aysel" not in by_username

    by_phone = client.get("/admin?q=0553334422", auth=AUTH).text
    assert "Aysel" in by_phone and "Elvin" not in by_phone

    by_name = client.get("/admin?q=Aysel", auth=AUTH).text
    assert "Aysel" in by_name and "Elvin" not in by_name


def test_lead_detail_page(client, settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "user-9", "m-9", "Elvin 0501234567", username="elvin_m")
    response = client.get(f"/admin/lead/{ACCOUNT_A}/user-9", auth=AUTH)
    assert response.status_code == 200
    assert "tel:+994501234567" in response.text
    assert "https://instagram.com/elvin_m" in response.text
    assert "Söhbət" in response.text

    assert client.get(f"/admin/lead/{ACCOUNT_A}/nobody", auth=AUTH).status_code == 404
    assert client.get(f"/admin/lead/{ACCOUNT_A}/user-9").status_code == 401


def test_csv_contains_username_and_instagram_id(client, settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "user-9", "m-9", "Elvin 0501234567", username="elvin_m")
    text = client.get("/admin/leads.csv", auth=AUTH).content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert "instagram_id" in header and "username" in header
    assert "elvin_m" in text and "user-9" in text


def test_timeline_shows_customer_and_bot_chronologically(client, settings):
    from leadsbot import storage

    storage.intake(settings.database_path, ACCOUNT_A, "user-t", "m-1", "Salam")
    storage.intake(settings.database_path, ACCOUNT_A, "user-t", "m-2", "Elvin 0501234567")

    page = client.get(f"/admin/lead/{ACCOUNT_A}/user-t", auth=AUTH).text
    assert "Müştəri" in page          # inbound messages are visible to managers
    assert "Elvin 0501234567" in page
    assert page.index("Salam") < page.index("Elvin 0501234567")  # oldest first