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
    letters = new_client().post(f"{API}/auth/signup", json={"email": "kofi@example.com", "password": "onlyletters"})
    assert letters.status_code == 400 and "number" in letters.json()["detail"]


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
    for _ in range(4):
        r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "wrong"})
        # A wrong password says only that; no countdown of attempts.
        assert r.status_code == 401 and r.json()["detail"] == "Incorrect email or password."
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "wrong"})
    assert r.status_code == 423 and r.json()["detail"].startswith("Too many attempts. You can sign in again at ")
    assert r.json()["until"]  # for the sign-in page's countdown
    # Even the right password is refused while locked.
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert r.status_code == 423
    # There is no public endpoint telling anyone how many tries an email has left.
    assert c.get(f"{API}/admin/auth/lockout", params={"email": "superadmin@agrictec.app"}).status_code in (404, 405)


def test_a_strangers_failures_never_lock_the_admin_out(monkeypatch):
    from app.config import get_settings

    secret = "s" * 40
    monkeypatch.setattr(get_settings(), "frontend_proxy_secret", secret)

    def from_address(ip: str) -> dict:
        return {"x-agrictec-proxy-secret": secret, "x-agrictec-client-ip": ip}

    stranger = new_client()
    for _ in range(5):
        stranger.post(f"{API}/admin/auth/login", headers=from_address("203.0.113.9"),
                      json={"email": "superadmin@agrictec.app", "password": "wrong"})
    locked = stranger.post(f"{API}/admin/auth/login", headers=from_address("203.0.113.9"),
                           json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert locked.status_code == 423
    owner = new_client().post(f"{API}/admin/auth/login", headers=from_address("198.51.100.7"),
                              json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert owner.status_code == 200


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


def test_a_farmer_unused_for_a_week_is_signed_out(db):
    c = signup()
    assert c.get(f"{API}/auth/session").status_code == 200
    db.execute(
        update(AuthSession)
        .where(AuthSession.kind == "farmer")
        .values(last_activity_at=AuthSession.last_activity_at - timedelta(days=8))
    )
    db.commit()
    r = c.get(f"{API}/auth/session")
    assert r.status_code == 401 and r.json()["code"] == "session_expired"
    # Ended for good: it doesn't come back.
    assert c.get(f"{API}/auth/session").status_code == 401


def test_farmer_lockout_after_five_wrong_passwords_says_when_to_try_again():
    signup("ama@example.com", "password123")
    c = new_client()
    for _ in range(4):
        r = c.post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "wrong-password"})
        assert r.status_code == 401 and r.json()["detail"] == "Incorrect email or password"
    r = c.post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "wrong-password"})
    assert r.status_code == 423 and r.json()["code"] == "locked"
    assert r.json()["detail"].startswith("Too many attempts. You can sign in again at ")
    assert r.json()["until"]
    # Even the right password is refused until then.
    assert c.post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "password123"}).status_code == 423


def test_a_right_password_clears_the_farmers_failures():
    signup("kofi@example.com", "password123")
    c = new_client()
    for _ in range(4):
        c.post(f"{API}/auth/login", json={"email": "kofi@example.com", "password": "wrong-password"})
    assert c.post(f"{API}/auth/login", json={"email": "kofi@example.com", "password": "password123"}).status_code == 200
    for _ in range(4):  # a fresh set of five
        r = c.post(f"{API}/auth/login", json={"email": "kofi@example.com", "password": "wrong-password"})
        assert r.status_code == 401


def test_the_lock_message_names_the_minute_it_ends_never_earlier():
    from datetime import UTC, datetime

    from app.services.login_lockout import locked_message

    now = datetime(2026, 11, 11, 14, 30, 20, tzinfo=UTC)
    until = datetime(2026, 11, 11, 14, 35, 20, tzinfo=UTC)
    assert locked_message(until, now) == "Too many attempts. You can sign in again at 14:36 GMT (in 5 minutes)."
    on_the_minute = datetime(2026, 11, 11, 14, 35, tzinfo=UTC)
    assert "at 14:35 GMT (in 5 minutes)" in locked_message(on_the_minute, datetime(2026, 11, 11, 14, 30, tzinfo=UTC))
    assert "(in 1 minute)" in locked_message(on_the_minute, datetime(2026, 11, 11, 14, 34, 30, tzinfo=UTC))
