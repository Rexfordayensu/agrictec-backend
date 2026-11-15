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
