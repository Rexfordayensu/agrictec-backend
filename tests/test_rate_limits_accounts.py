"""Rate limits counted per account (farmers on one mobile network share an address; an admin
team shares an office's), the admin password change's lockout, and the sign-in allowance."""

from tests.conftest import ADMIN_PASSWORD, API, new_client, signup, super_admin


def test_a_wrong_current_password_counts_toward_the_admin_lockout():
    boss = super_admin()
    wrong = {"currentPassword": "not-it", "newPassword": "An0ther$ecret!"}
    codes = [boss.post(f"{API}/admin/auth/change-password", json=wrong).status_code for _ in range(5)]
    assert codes == [400] * 4 + [423], "the fifth wrong password in a row locks"
    locked = boss.post(f"{API}/admin/auth/change-password", json={**wrong, "currentPassword": ADMIN_PASSWORD})
    assert locked.status_code == 423 and locked.json()["until"], "even the right password waits"
    # The same lock holds at sign-in from this address.
    signin = new_client().post(
        f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": ADMIN_PASSWORD}
    )
    assert signin.status_code == 423


def test_the_right_current_password_clears_earlier_mistakes():
    boss = super_admin()
    for _ in range(4):
        assert boss.post(
            f"{API}/admin/auth/change-password", json={"currentPassword": "not-it", "newPassword": "An0ther$ecret!"}
        ).status_code == 400
    changed = boss.post(
        f"{API}/admin/auth/change-password",
        json={"currentPassword": ADMIN_PASSWORD, "newPassword": "An0ther$ecret!"},
    )
    assert changed.status_code == 200, changed.text
    # A single new mistake doesn't lock: the count started again.
    assert boss.post(
        f"{API}/admin/auth/change-password", json={"currentPassword": "not-it", "newPassword": "Th1rd$ecret!"}
    ).status_code == 400


def test_admin_password_changes_are_rate_limited_per_admin():
    boss = super_admin()  # its own change counts as the first
    codes = [
        boss.post(
            f"{API}/admin/auth/change-password", json={"currentPassword": "x", "newPassword": "An0ther$ecret!"}
        ).status_code
        for _ in range(10)
    ]
    assert 429 not in codes[:9] and codes[9] == 429


def test_payment_checkout_is_limited_per_farmer_not_per_address():
    ama, kofi = signup("ama@example.com"), signup("kofi@example.com")  # same address
    body = {"planId": "basic", "method": "mobile-money"}
    codes = [ama.post(f"{API}/me/subscription/checkout", json=body).status_code for _ in range(11)]
    assert 429 not in codes[:10] and codes[10] == 429
    assert kofi.post(f"{API}/me/subscription/checkout", json=body).status_code != 429


def test_verification_requests_are_limited_per_farmer():
    ama = signup()
    body = {"kind": "phone", "value": "not a phone"}
    codes = [ama.post(f"{API}/me/verifications", json=body).status_code for _ in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429


def test_admin_exports_are_limited_per_admin():
    boss = super_admin()
    codes = [boss.post(f"{API}/admin/audit/export", json={"format": "json"}).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_farmer_sign_in_allows_sixty_tries_per_address_in_fifteen_minutes():
    # Different emails, so the per-account lockout never applies: only the address allowance.
    client = new_client()
    codes = [
        client.post(f"{API}/auth/login", json={"email": f"farmer{i}@example.com", "password": "wrong-pass1"}).status_code
        for i in range(61)
    ]
    assert codes[:60] == [401] * 60 and codes[60] == 429
