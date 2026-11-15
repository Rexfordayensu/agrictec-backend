"""A farmer can close their own account: with their password and DELETE typed, the sign-in and
everything in it go at once, and every device is signed out. Staff viewing as the farmer can't."""

from tests.conftest import API, ONBOARDING, add_record, new_client, signup, super_admin


def test_a_farmer_closes_their_account():
    ama = signup("closing@example.com")
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    add_record(ama, {"type": "income", "description": "Maize sale", "amount": 50, "crop": "Maize",
                     "date": "2026-11-02", "paymentMethod": "cash"})
    phone = new_client()
    assert phone.post(f"{API}/auth/login", json={"email": "closing@example.com", "password": "password123"}).status_code == 200

    wrong = ama.post(f"{API}/me/delete-account", json={"password": "nope", "confirmation": "DELETE"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "bad_password"
    unconfirmed = ama.post(f"{API}/me/delete-account", json={"password": "password123", "confirmation": "delete"})
    assert unconfirmed.status_code == 400
    assert ama.get(f"{API}/me/records").status_code == 200  # nothing happened yet

    closed = ama.post(f"{API}/me/delete-account", json={"password": "password123", "confirmation": "DELETE"})
    assert closed.status_code == 200, closed.text
    assert ama.get(f"{API}/auth/session").status_code == 401
    assert phone.get(f"{API}/me/records").status_code == 401
    again = new_client().post(f"{API}/auth/login", json={"email": "closing@example.com", "password": "password123"})
    assert again.status_code == 401
    boss = super_admin()
    assert all(f["email"] != "closing@example.com" for f in boss.get(f"{API}/admin/farmers/page").json()["items"])
    # The email is free to sign up again, as a new account.
    assert signup("closing@example.com").get(f"{API}/me/records").json() == []


def test_staff_viewing_as_the_farmer_cannot_close_it():
    boss = super_admin()
    farmer = signup("kept@example.com")
    uid = farmer.get(f"{API}/auth/session").json()["userId"]
    start = boss.post(f"{API}/admin/farmers/{uid}/impersonate").json()
    token = boss.post(f"{API}/auth/impersonation/claim", json={"token": start["token"]}).json()["token"]
    r = boss.post(f"{API}/me/delete-account", headers={"X-Impersonation-Token": token},
                  json={"password": "password123", "confirmation": "DELETE"})
    assert r.status_code == 403
    assert farmer.get(f"{API}/auth/session").status_code == 200
