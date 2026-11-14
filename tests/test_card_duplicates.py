"""One Ghana Card belongs to one person: once it is verified on one account, the same card can't be
approved on another, and the reviewer sees it before deciding."""

from tests.conftest import API, ONBOARDING, signup, super_admin

CARD = {"ghanaCard": {"cardNumber": "GHA-123456789-0"}}


def _with_card(email):
    farmer = signup(email)
    assert farmer.post(f"{API}/me/onboarding/complete", json={**ONBOARDING, **CARD}).status_code == 200
    return farmer


def _pending(boss, farmer):
    me = farmer.get(f"{API}/me/profile").json()["userId"]
    items = boss.get(f"{API}/admin/verifications", params={"status": "pending", "kind": "ghanaCard"}).json()
    return next(i for i in items if i["userId"] == me)


def test_the_same_card_cannot_be_verified_on_a_second_account():
    boss = super_admin()
    ama, kofi = _with_card("ama.card@example.com"), _with_card("kofi.card@example.com")
    first = _pending(boss, ama)
    assert first["cardOnOtherAccounts"] == 0
    assert boss.post(f"{API}/admin/verifications/{first['id']}/approve").status_code == 200

    second = _pending(boss, kofi)
    assert second["cardOnOtherAccounts"] == 1  # shown to the reviewer before deciding
    refused = boss.post(f"{API}/admin/verifications/{second['id']}/approve")
    assert refused.status_code == 409 and refused.json()["code"] == "card_on_other_account"
    assert kofi.get(f"{API}/me/profile").json()["ghanaCard"]["status"] == "pending"
    # The reviewer can still reject it.
    rejected = boss.post(f"{API}/admin/verifications/{second['id']}/reject",
                         json={"reason": "Other", "note": "This card is verified on another account."})
    assert rejected.status_code == 200
