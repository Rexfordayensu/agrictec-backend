"""A Ghana Card is reviewed against the farmer's name. Changing the name afterwards sends the card
back to review, so a lender never sees a new name beside a card that was checked for another.
Capitals and spacing don't count as a change."""

from tests.conftest import API, ONBOARDING, signup, super_admin


def card_status(client) -> str:
    return client.get(f"{API}/me/profile").json()["ghanaCard"]["status"]


def approve_card(boss, client) -> None:
    uid = client.get(f"{API}/auth/session").json()["userId"]
    request = next(r for r in boss.get(f"{API}/admin/farmers/{uid}/verifications").json()
                   if r["status"] == "pending" and r["kind"] == "ghanaCard")
    assert boss.post(f"{API}/admin/verifications/{request['id']}/approve").status_code == 200


def save(client, **changes) -> dict:
    profile = {**client.get(f"{API}/me/profile").json(), **changes}
    r = client.put(f"{API}/me/profile", json=profile)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_new_name_sends_an_approved_card_back_to_review():
    boss = super_admin()
    ama = signup("rename@example.com")
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    assert ama.post(f"{API}/me/verifications", json={"kind": "ghanaCard", "value": "GHA-333333333-3"}).status_code == 201
    approve_card(boss, ama)
    assert card_status(ama) == "verified"

    # Capitals and spacing only: still the same name, still verified.
    save(ama, name="  ama   MENSAH ")
    assert card_status(ama) == "verified"

    out = save(ama, name="Akosua Mensah")
    assert out["ghanaCard"]["status"] == "pending"
    assert out["verification"]["ghanaCard"] is False
    uid = ama.get(f"{API}/auth/session").json()["userId"]
    waiting = [r for r in boss.get(f"{API}/admin/farmers/{uid}/verifications").json()
               if r["status"] == "pending" and r["kind"] == "ghanaCard"]
    assert len(waiting) == 1 and waiting[0]["kind"] == "ghanaCard"
    assert "Akosua Mensah" in waiting[0]["note"] and "MENSAH" in waiting[0]["note"].upper()
    # The same masked card, reviewed again for the new name.
    approve_card(boss, ama)
    assert card_status(ama) == "verified"


def test_a_name_change_without_an_approved_card_changes_nothing_else():
    ama = signup("noname.card@example.com")
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    save(ama, name="Yaa Mensah")
    assert card_status(ama) == "not_provided"
