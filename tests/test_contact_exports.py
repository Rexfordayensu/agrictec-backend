"""Farmers' contact details and farm pins leave the console in bulk only for Admin and Super
admin: an Analyst's farmers CSV and report-builder exports leave them out (the server decides,
whatever columns are asked for)."""

from tests.conftest import API, ONBOARDING, signup, super_admin
from tests.test_admin import admin_with_role


def test_an_analyst_exports_farmers_without_contacts_an_admin_with_them():
    farmer = signup("contact.export@example.com")
    farmer.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    boss = super_admin()
    analyst = admin_with_role(boss, "analyst")
    admin = admin_with_role(boss, "admin")

    def header(client) -> list[str]:
        r = client.post(f"{API}/admin/farmers/export", json={})
        assert r.status_code == 200, r.text
        return r.text.lstrip("\ufeff").splitlines()[0].split(",")

    for hidden in ("Email", "Phone", "GPS latitude", "Community"):
        assert hidden not in header(analyst)
        assert hidden in header(admin)
    assert "Region" in header(analyst) and "contact.export@example.com" not in analyst.post(
        f"{API}/admin/farmers/export", json={}).text

    spec = {"dataset": "farmers", "columns": ["name", "email", "phone", "region"]}
    preview = analyst.post(f"{API}/admin/reports/builder/preview", json=spec).json()
    assert "Email" not in preview["headers"] and "Phone" not in preview["headers"] and "Region" in preview["headers"]
    assert "Email" in admin.post(f"{API}/admin/reports/builder/preview", json=spec).json()["headers"]
    # The builder's column list follows the same rule.
    columns = {c["key"] for d in analyst.get(f"{API}/admin/reports").json()["datasets"] if d["id"] == "farmers"
               for c in d["columns"]}
    assert "email" not in columns and "phone" not in columns and "region" in columns
