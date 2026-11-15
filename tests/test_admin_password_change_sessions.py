"""Changing a staff password signs that account out everywhere else at once: a stolen password
stops working. The session that made the change stays signed in."""

from tests.conftest import ADMIN_PASSWORD, API, new_client, super_admin

EMAIL = "superadmin@agrictec.app"


def test_a_password_change_ends_the_other_sessions_only():
    here = super_admin()
    elsewhere = new_client()
    assert elsewhere.post(f"{API}/admin/auth/login", json={"email": EMAIL, "password": ADMIN_PASSWORD}).status_code == 200
    assert elsewhere.get(f"{API}/admin/farmers/page").status_code == 200

    new_password = "An0ther-Str0ng-Passw0rd!"
    changed = here.post(f"{API}/admin/auth/change-password",
                        json={"currentPassword": ADMIN_PASSWORD, "newPassword": new_password})
    assert changed.status_code == 200, changed.text
    assert here.get(f"{API}/admin/farmers/page").status_code == 200
    assert elsewhere.get(f"{API}/admin/farmers/page").status_code == 401
