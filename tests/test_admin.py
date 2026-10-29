"""The admin console: roles, the audit trail, and every workflow's rules."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import API, ONBOARDING, admin_with_role, audit_count, new_client, signup, super_admin

LOAN = {"partnerId": "sinapi", "amount": 1000, "purpose": "Farm Equipment", "months": 12}


def farmer_id(c) -> str:
    return c.get(f"{API}/auth/session").json()["userId"]


@pytest.mark.parametrize(
    ("role", "allowed", "refused"),
    [
        ("analyst", ["/admin/overview", "/admin/farmers", "/admin/loan-applications", "/admin/reports"],
         ["/admin/verifications", "/admin/audit", "/admin/team", "/admin/risk/flags", "/admin/content/crops"]),
        ("support", ["/admin/verifications", "/admin/risk/flags"], ["/admin/audit", "/admin/reports", "/admin/lenders"]),
        ("admin", ["/admin/audit", "/admin/lenders", "/admin/content/crops", "/admin/announcements"],
         ["/admin/team", "/admin/credit-config/history"]),
    ],
)
def test_each_role_reaches_only_its_pages(role, allowed, refused):
    c = admin_with_role(super_admin(), role)
    for path in allowed:
        assert c.get(f"{API}{path}").status_code == 200, path
    for path in refused:
        r = c.get(f"{API}{path}")
        assert r.status_code == 403 and r.json()["detail"].startswith("You do not have permission"), path


def test_allowed_writes_log_once_and_refused_writes_change_nothing(db):
    boss = super_admin()
    farmer = signup()
    uid = farmer_id(farmer)
    analyst = admin_with_role(boss, "analyst")

    before = audit_count(db)
    assert analyst.post(f"{API}/admin/farmers/{uid}/suspend", json={"reason": "Fake records"}).status_code == 403
    assert audit_count(db) == before
    assert farmer.get(f"{API}/auth/session").status_code == 200

    assert boss.post(f"{API}/admin/farmers/{uid}/suspend", json={"reason": "Fake records"}).status_code == 200
    assert audit_count(db) == before + 1
    # The open session ends at once, and signing in is refused.
    r = farmer.get(f"{API}/auth/session")
    assert r.status_code == 401
    r = new_client().post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "password123"})
    assert r.status_code == 403 and r.json()["detail"] == "Your account is suspended. Contact support."

    # A failed write (already suspended) logs nothing either.
    assert boss.post(f"{API}/admin/farmers/{uid}/suspend", json={"reason": "Again"}).status_code == 400
    assert audit_count(db) == before + 1


def test_the_audit_log_is_append_only_in_the_database(db):
    super_admin()
    assert audit_count(db) > 0
    for statement in ("update audit_log set summary = 'x'", "delete from audit_log", "truncate audit_log"):
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(statement))
        db.rollback()


def test_audit_entries_never_hold_secrets():
    boss = super_admin()
    farmer = signup()
    temp = boss.post(f"{API}/admin/farmers/{farmer_id(farmer)}/reset-password").json()["temporaryPassword"]
    exported = boss.post(f"{API}/admin/audit/export", json={"format": "json"}).json()["content"]
    assert temp not in exported and "argon2" not in exported

    # The farmer must replace the temporary password before any session starts.
    r = new_client().post(f"{API}/auth/login", json={"email": "ama@example.com", "password": temp})
    assert r.status_code == 409 and r.json()["code"] == "password_change_required"
    r = new_client().post(
        f"{API}/auth/replace-temporary-password",
        json={"email": "ama@example.com", "temporaryPassword": temp, "newPassword": "brand-new-pass"},
    )
    assert r.status_code == 200


def test_ghana_card_review_flow():
    boss = super_admin()
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json={**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-111111111-1"}})
    request = boss.get(f"{API}/admin/verifications", params={"status": "pending"}).json()[0]
    assert boss.post(f"{API}/admin/verifications/approve-many", json={"ids": [request["id"]]}).status_code == 400
    r = boss.post(f"{API}/admin/verifications/{request['id']}/reject", json={"reason": "Other"})
    assert r.status_code == 400 and "note" in r.json()["detail"]
    boss.post(f"{API}/admin/verifications/{request['id']}/reject", json={"reason": "Unclear", "note": "Blurry photo."})
    card = farmer.get(f"{API}/me/profile").json()["ghanaCard"]
    assert card["status"] == "rejected" and card["rejectedReason"] == "Rejected: Unclear. Please resubmit. Blurry photo."
    assert boss.post(f"{API}/admin/verifications/{request['id']}/approve").status_code == 400
    # The farmer can resubmit.
    assert farmer.post(f"{API}/me/verifications", json={"kind": "ghanaCard", "value": "GHA-111111111-1"}).status_code == 201


def test_loan_pipeline_with_the_demo_world():
    boss = super_admin()
    reset = boss.post(f"{API}/admin/system/reset-demo", json={"confirmation": "RESET"})
    assert reset.status_code == 200 and reset.json()["farmers"] == 60
    apps = boss.get(f"{API}/admin/loan-applications", params={"status": "submitted"}).json()
    app = apps[0]
    r = boss.post(f"{API}/admin/loan-applications/{app['id']}/status", json={"status": "disbursed"})
    assert r.status_code == 400 and "can’t move" in r.json()["detail"]
    moved = boss.post(f"{API}/admin/loan-applications/{app['id']}/status", json={"status": "under_review"}).json()
    assert moved["status"] == "under_review" and moved["history"][-1]["by"] == "Super Admin"
    assert boss.post(f"{API}/admin/loan-applications/{app['id']}/status", json={"status": "approved"}).status_code == 400
    approved = boss.post(f"{API}/admin/loan-applications/{app['id']}/status", json={"status": "approved", "note": "Good records."})
    assert approved.status_code == 200
    review = boss.get(f"{API}/admin/loan-applications/{app['id']}/review").json()
    assert review["farmerExists"] and {c["key"] for c in review["checks"]} >= {"records", "score", "term"}

    # A lender with applications under review can't be switched off, nor deleted.
    reviewing = boss.get(f"{API}/admin/loan-applications", params={"status": "under_review"}).json()[0]
    r = boss.post(f"{API}/admin/lenders/{reviewing['partnerId']}/toggle")
    assert r.status_code == 400 and "under review" in r.json()["detail"]
    assert boss.delete(f"{API}/admin/lenders/{reviewing['partnerId']}").status_code == 400

    csv = boss.post(f"{API}/admin/loan-applications/export", json={})
    assert csv.status_code == 200 and csv.text.startswith("﻿Reference,Farmer")


def test_credit_rules_are_versioned_and_drive_the_score():
    boss = super_admin()
    live = new_client().get(f"{API}/platform").json()["creditConfig"]
    draft = {k: live[k] for k in ("weights", "targets", "bands", "eligibility")}
    assert boss.post(f"{API}/admin/credit-config/publish", json={"config": draft, "note": "No change at all"}).status_code == 400
    draft["eligibility"] = {**draft["eligibility"], "minScore": 600}
    assert boss.post(f"{API}/admin/credit-config/publish", json={"config": draft, "note": "short"}).status_code == 400
    impact = boss.post(f"{API}/admin/credit-config/impact", json={"draft": draft})
    assert impact.status_code == 200
    v2 = boss.post(f"{API}/admin/credit-config/publish", json={"config": draft, "note": "Raise the minimum score"}).json()
    assert v2["version"] == 2 and v2["note"] == "Raise the minimum score"
    restored = boss.post(f"{API}/admin/credit-config/restore", json={"version": 1}).json()
    assert restored["version"] == 3 and restored["restoredFrom"] == 1
    assert [c["version"] for c in boss.get(f"{API}/admin/credit-config/history").json()] == [2, 1]


def test_team_rules():
    boss = super_admin()
    me = boss.get(f"{API}/admin/auth/session").json()["admin"]["id"]
    r = boss.post(f"{API}/admin/team/{me}/role", json={"role": "admin"})
    assert r.status_code == 403 and "your own access" in r.json()["detail"]
    other = admin_with_role(boss, "super_admin", "second@agrictec.app")
    # Two super admins: the second may demote the first, but then nobody can demote the second.
    assert other.post(f"{API}/admin/team/{me}/role", json={"role": "admin"}).status_code == 200
    farmer_email = signup().get(f"{API}/auth/session").json()["email"]
    r = other.post(f"{API}/admin/team", json={"name": "Clash", "email": farmer_email, "role": "analyst"})
    assert r.status_code == 409 and "farmer account" in r.json()["detail"]


def test_view_as_farmer_is_read_only_and_audited(db):
    boss = super_admin()
    farmer = signup()
    farmer.post(f"{API}/me/records", json={"type": "activity", "description": "Weeded", "crop": "Maize", "date": "2026-10-03"})
    uid = farmer_id(farmer)
    start = boss.post(f"{API}/admin/farmers/{uid}/impersonate").json()
    claimed = boss.post(f"{API}/auth/impersonation/claim", json={"token": start["token"]})
    assert claimed.status_code == 200 and claimed.json()["session"]["readOnly"] is True
    view = {"X-Impersonation-Token": claimed.json()["token"]}
    assert len(boss.get(f"{API}/me/records", headers=view).json()) == 1
    r = boss.post(f"{API}/me/records", headers=view, json={"type": "activity", "description": "x", "crop": "Maize", "date": "2026-10-03"})
    assert r.status_code == 403 and r.json()["detail"] == "Read-only while viewing as farmer"
    # Single use.
    assert boss.post(f"{API}/auth/impersonation/claim", json={"token": start["token"]}).status_code == 400
    boss.post(f"{API}/auth/impersonation/end", headers=view)
    assert boss.get(f"{API}/me/records", headers=view).status_code == 401
    actions = [e["action"] for e in boss.get(f"{API}/admin/audit", params={"action": "farmer."}).json()["items"]]
    assert actions[:2] == ["farmer.impersonate_end", "farmer.impersonate"]


def test_deleting_a_farmer_keeps_their_loan_history(db):
    boss = super_admin()
    boss.post(f"{API}/admin/system/reset-demo", json={"confirmation": "RESET"})
    app = boss.get(f"{API}/admin/loan-applications").json()[0]
    farmer = boss.get(f"{API}/admin/farmers/{app['userId']}").json()
    assert boss.post(f"{API}/admin/farmers/{app['userId']}/delete", json={"confirmation": "someone else"}).status_code == 400
    assert boss.post(f"{API}/admin/farmers/{app['userId']}/delete", json={"confirmation": farmer["email"]}).status_code == 200
    assert boss.get(f"{API}/admin/farmers/{app['userId']}").status_code == 404
    review = boss.get(f"{API}/admin/loan-applications/{app['id']}/review").json()
    assert review["farmerExists"] is False
    r = boss.post(f"{API}/admin/loan-applications/{app['id']}/status", json={"status": "under_review"})
    assert app["status"] != "submitted" or "can only be closed" in r.json()["detail"]


def test_reports_risk_and_backup_round_trip():
    boss = super_admin()
    boss.post(f"{API}/admin/system/reset-demo", json={"confirmation": "RESET"})
    for report in ("platform", "regions", "loans", "credit", "verifications", "risk"):
        assert boss.get(f"{API}/admin/reports/{report}").status_code == 200, report
    csv = boss.post(f"{API}/admin/reports/regions/export", params={"format": "csv"})
    assert csv.status_code == 200 and "AgriCtec — Farmers by Region" in csv.text
    preview = boss.post(f"{API}/admin/reports/builder/preview", json={"dataset": "farmers", "columns": ["name", "score"], "region": "Ashanti"}).json()
    assert preview["total"] == 9 and preview["headers"] == ["Name", "Credit score"]

    flags = boss.get(f"{API}/admin/risk/flags", params={"status": "open"}).json()
    assert len({f["rule"] for f in flags}) == 7
    assert boss.post(f"{API}/admin/risk/scan").json() == []  # nothing new: findings are stored once
    related = boss.get(f"{API}/admin/risk/flags/{flags[0]['id']}/related").json()
    assert related["farmer"]["userId"] == flags[0]["userId"]
    confirmed = boss.post(f"{API}/admin/risk/flags/{flags[0]['id']}/confirm", json={"note": "Checked with lender", "suspend": True}).json()
    assert confirmed["status"] == "confirmed"
    assert boss.get(f"{API}/admin/farmers/{flags[0]['userId']}").json()["status"] == "suspended"

    backup = boss.post(f"{API}/admin/system/backup").json()
    assert "password_hash" not in str(backup["data"]["users"][0])
    summary = boss.post(f"{API}/admin/system/backup/summary", json=backup).json()
    assert summary["farmers"] == 60
    boss.post(f"{API}/admin/system/delete-farmers", json={"confirmation": "DELETE ALL FARMERS", "password": "Sup3r$ecret!"})
    assert boss.get(f"{API}/admin/farmers").json() == []
    assert boss.post(f"{API}/admin/system/restore", json={"backup": backup, "confirmation": "nope"}).status_code == 400
    restored = boss.post(f"{API}/admin/system/restore", json={"backup": backup, "confirmation": "RESTORE"})
    assert restored.status_code == 200, restored.text
    assert len(boss.get(f"{API}/admin/farmers").json()) == 60
    # Restored accounts keep the password they have on this server (none here, until a reset).
    r = new_client().post(f"{API}/auth/login", json={"email": backup["data"]["users"][0]["email"], "password": "Farmer@2026"})
    assert r.status_code == 401
