"""A signed-in farmer can change their password with a code emailed to their account's email (it
proves the email is theirs): the current password is checked, the new one follows the rules, the
code is single-use and limited, every other device is signed out and this one stays signed in."""

import pytest

from app.config import get_settings
from app.services import password_reset
from tests.conftest import API, new_client, signup


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def emailed(settings, monkeypatch):
    """Email "configured"; the codes that would have been emailed, by address."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "AgriCtec <no-reply@example.com>")
    sent: dict[str, list[str]] = {}

    class Now:  # run the "background" email straight away
        def __init__(self, target, args, **_):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(password_reset, "_send_change", lambda email, code: sent.setdefault(email, []).append(code))
    monkeypatch.setattr(password_reset.threading, "Thread", Now)
    return sent


def change(client, code, current="password123", new="brandnew123"):
    return client.post(f"{API}/me/password", json={"currentPassword": current, "newPassword": new, "code": code})


def test_a_farmer_changes_their_password_with_an_emailed_code(emailed):
    here = signup("newpass@example.com")
    phone = new_client()
    assert phone.post(f"{API}/auth/login", json={"email": "newpass@example.com", "password": "password123"}).status_code == 200

    sent = here.post(f"{API}/me/password/code")
    assert sent.status_code == 200, sent.text
    assert sent.json()["sentTo"] == "n******@example.com"
    code = emailed["newpass@example.com"][0]
    assert len(code) == 6 and code.isdigit()
    # A second request within a minute sends nothing new.
    assert here.post(f"{API}/me/password/code").status_code == 429

    wrong_code = "000000" if code != "000000" else "111111"
    assert change(here, wrong_code).json()["code"] == "invalid_code"
    bad_password = change(here, code, current="nope")
    assert bad_password.status_code == 400 and bad_password.json()["code"] == "bad_password"
    assert change(here, code, new="short").status_code == 400
    assert change(here, code, new="password123").status_code == 400

    done = change(here, code)
    assert done.status_code == 200, done.text
    assert here.get(f"{API}/auth/session").status_code == 200  # this device stays signed in
    assert phone.get(f"{API}/auth/session").status_code == 401  # the other one is signed out
    assert new_client().post(f"{API}/auth/login", json={"email": "newpass@example.com", "password": "brandnew123"}).status_code == 200
    # The code worked once only.
    assert change(here, code, current="brandnew123", new="another123").json()["code"] == "invalid_code"
    titles = [n["title"] for n in here.get(f"{API}/me/notifications").json()["items"]]
    assert "Your password was changed" in titles


def test_without_a_code_nothing_changes(emailed):
    here = signup("nocode@example.com")
    r = change(here, "123456")
    assert r.status_code == 400 and r.json()["code"] == "invalid_code"
    assert new_client().post(f"{API}/auth/login", json={"email": "nocode@example.com", "password": "password123"}).status_code == 200


def test_five_wrong_codes_use_it_up(emailed):
    here = signup("guessing@example.com")
    here.post(f"{API}/me/password/code")
    code = emailed["guessing@example.com"][0]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(password_reset.MAX_ATTEMPTS):
        change(here, wrong)
    assert change(here, code).json()["code"] == "invalid_code"


def test_the_live_site_without_email_says_codes_cannot_be_sent(settings, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "smtp_host", None)
    here = signup("noemail@example.com")
    r = here.post(f"{API}/me/password/code")
    assert r.status_code == 503 and r.json()["code"] == "email_unavailable"
