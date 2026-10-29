"""Sign-in, sessions and their separation (farmer vs admin)."""

from datetime import timedelta

from sqlalchemy import update

from app.models import AuthSession
from tests.conftest import ADMIN_PASSWORD, API, new_client, signup, super_admin


def test_signup_signin_and_signout():
    c = signup()
    assert c.cookies.get("agrictec_session")
    assert c.get(f"{API}/auth/session").json()["email"] == "ama@example.com"
    assert c.post(f"{API}/auth/logout").status_code == 200
    assert c.get(f"{API}/auth/session").status_code == 401

    again = new_client()
    assert again.post(f"{API}/auth/login", json={"email": "AMA@example.com ", "password": "password123"}).status_code == 200
    wrong = new_client().post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "nope"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Incorrect email or password"


def test_duplicate_email_and_short_password():
    signup()
    r = new_client().post(f"{API}/auth/signup", json={"email": "ama@example.com", "password": "password123"})
    assert r.status_code == 409
    r = new_client().post(f"{API}/auth/signup", json={"email": "kofi@example.com", "password": "short"})
    assert r.status_code == 422


def test_bearer_token_works_without_cookies():
    r = new_client().post(f"{API}/auth/signup", json={"email": "api@example.com", "password": "password123"})
    token = r.json()["token"]
    bare = new_client()
    assert bare.get(f"{API}/auth/session", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_guest_is_created_once_per_device():
    c = new_client()
    first = c.post(f"{API}/auth/guest").json()["session"]["userId"]
    c.post(f"{API}/auth/logout")
    second = c.post(f"{API}/auth/guest").json()["session"]["userId"]
    assert first == second
    other_device = new_client().post(f"{API}/auth/guest").json()["session"]["userId"]
    assert other_device != first


def test_signup_and_guest_follow_the_flags():
    boss = super_admin()
    flags = new_client().get(f"{API}/platform/flags").json()
    assert boss.put(f"{API}/admin/settings/flags", json={**flags, "signupOpen": False, "guestMode": False}).status_code == 200
    assert new_client().post(f"{API}/auth/signup", json={"email": "x@example.com", "password": "password123"}).status_code == 403
    assert new_client().post(f"{API}/auth/guest").status_code == 403


def test_a_farmer_session_never_opens_the_console():
    c = signup()
    assert c.get(f"{API}/admin/overview").status_code == 401
    token = c.cookies.get("agrictec_session")
    assert new_client().get(f"{API}/admin/overview", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_admin_must_change_the_demo_password_first():
    c = new_client()
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert r.json()["session"]["admin"]["mustChangePassword"] is True
    blocked = c.get(f"{API}/admin/farmers")
    assert blocked.status_code == 403 and blocked.json()["code"] == "password_change_required"
    weak = c.post(f"{API}/admin/auth/change-password", json={"currentPassword": "Admin@2026", "newPassword": "weakpassword"})
    assert weak.status_code == 400 and "uppercase" in weak.json()["detail"]
    assert c.post(f"{API}/admin/auth/change-password", json={"currentPassword": "Admin@2026", "newPassword": ADMIN_PASSWORD}).status_code == 200
    assert c.get(f"{API}/admin/farmers").status_code == 200


def test_admin_lockout_after_five_failures():
    c = new_client()
    for left in (4, 3, 2, 1):
        r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "wrong"})
        assert r.status_code == 401 and f"{left} attempt" in r.json()["detail"]
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "wrong"})
    assert r.status_code == 423 and "Too many attempts" in r.json()["detail"]
    # Even the right password is refused while locked.
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert r.status_code == 423


def test_admin_idle_timeout_signs_out_and_is_logged(db):
    c = super_admin()
    db.execute(update(AuthSession).where(AuthSession.kind == "admin").values(last_activity_at=AuthSession.last_activity_at - timedelta(minutes=31)))
    db.commit()
    r = c.get(f"{API}/admin/auth/session")
    assert r.status_code == 401 and r.json()["code"] == "session_expired"
    again = new_client()
    again.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": ADMIN_PASSWORD})
    audit = again.get(f"{API}/admin/audit", params={"action": "admin.logout"}).json()
    assert any("after inactivity" in e["summary"] for e in audit["items"])


def test_signing_out_of_one_side_keeps_the_other():
    c = signup()
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert r.status_code == 200
    assert c.post(f"{API}/admin/auth/logout").status_code == 200
    assert c.get(f"{API}/auth/session").status_code == 200
