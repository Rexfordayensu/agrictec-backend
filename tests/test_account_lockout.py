"""Guessing one account's password from many addresses: each address stays under its own lock,
so the account itself counts every wrong password too. Thirty in a row lock it for 15 minutes
(even the right password waits); a password reset by the owner lifts it."""

from app.services import login_lockout
from tests.conftest import API, new_client, signup

EMAIL = "guessed.account@example.com"


def from_address(secret: str, ip: str) -> dict:
    return {"x-agrictec-proxy-secret": secret, "x-agrictec-client-ip": ip}


def test_many_addresses_together_lock_the_account(monkeypatch):
    from app.config import get_settings

    secret = "s" * 40
    monkeypatch.setattr(get_settings(), "frontend_proxy_secret", secret)
    signup(EMAIL)
    guesser = new_client()
    # Four wrong passwords from each address: never enough for the per-address lock.
    statuses = []
    for n in range(login_lockout.ACCOUNT_MAX_FAILURES):
        ip = f"198.51.100.{n // 4 + 1}"
        r = guesser.post(f"{API}/auth/login", headers=from_address(secret, ip),
                         json={"email": EMAIL, "password": f"wrong-{n}"})
        statuses.append(r.status_code)
    assert statuses[:-1] == [401] * (login_lockout.ACCOUNT_MAX_FAILURES - 1)
    assert statuses[-1] == 423
    # A fresh address, even with the right password, waits for the lock.
    owner = new_client()
    r = owner.post(f"{API}/auth/login", headers=from_address(secret, "192.0.2.50"),
                   json={"email": EMAIL, "password": "password123"})
    assert r.status_code == 423 and r.json()["until"]
    # Another account is unaffected.
    signup("someone.else@example.com")
    ok = owner.post(f"{API}/auth/login", headers=from_address(secret, "192.0.2.50"),
                    json={"email": "someone.else@example.com", "password": "password123"})
    assert ok.status_code == 200


def test_the_staff_sign_in_has_the_same_account_wide_limit(monkeypatch):
    from app.config import get_settings

    secret = "s" * 40
    monkeypatch.setattr(get_settings(), "frontend_proxy_secret", secret)
    guesser = new_client()
    last = None
    for n in range(login_lockout.ACCOUNT_MAX_FAILURES):
        last = guesser.post(f"{API}/admin/auth/login", headers=from_address(secret, f"203.0.113.{n // 4 + 1}"),
                            json={"email": "superadmin@agrictec.app", "password": f"wrong-{n}"})
    assert last.status_code == 423


def test_a_device_that_signed_in_before_is_not_held_by_the_account_wide_lock(monkeypatch):
    """Someone guessing from many addresses can't keep the owner out: the owner's own device
    (it signed in before) still gets in; a new device waits for the lock."""
    from app.config import get_settings

    secret = "s" * 40
    monkeypatch.setattr(get_settings(), "frontend_proxy_secret", secret)
    owner = new_client()
    first = owner.post(f"{API}/admin/auth/login", headers=from_address(secret, "192.0.2.77"),
                       json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert first.status_code == 200
    assert owner.cookies.get("agrictec_known_admin")
    owner.post(f"{API}/admin/auth/logout")

    guesser = new_client()
    for n in range(login_lockout.ACCOUNT_MAX_FAILURES):
        last = guesser.post(f"{API}/admin/auth/login", headers=from_address(secret, f"203.0.113.{n // 4 + 1}"),
                            json={"email": "superadmin@agrictec.app", "password": f"wrong-{n}"})
    assert last.status_code == 423
    stranger = new_client().post(f"{API}/admin/auth/login", headers=from_address(secret, "192.0.2.99"),
                                 json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert stranger.status_code == 423
    back = owner.post(f"{API}/admin/auth/login", headers=from_address(secret, "192.0.2.77"),
                      json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert back.status_code == 200, back.text


def test_a_known_device_cookie_for_another_account_counts_for_nothing():
    signup("first.device@example.com")
    signup("second.device@example.com")
    c = new_client()
    assert c.post(f"{API}/auth/login", json={"email": "first.device@example.com", "password": "password123"}).status_code == 200
    cookie = c.cookies.get("agrictec_known_device")
    assert login_lockout.is_known_device(cookie, "first.device@example.com", "farmer")
    assert not login_lockout.is_known_device(cookie, "second.device@example.com", "farmer")
    assert not login_lockout.is_known_device(cookie, "first.device@example.com", "admin")
    assert not login_lockout.is_known_device("forged.value", "first.device@example.com", "farmer")
