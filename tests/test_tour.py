"""The 6-step app tour opens once for a new farmer: finishing or skipping it is kept on the account
(POST /me/tour), so it doesn't open again on another phone, and saving settings never brings it
back."""

from tests.conftest import API, signup


def test_a_new_farmer_has_not_seen_the_tour_until_they_finish_or_skip_it():
    farmer = signup()
    assert farmer.get(f"{API}/me/settings").json()["tourSeenAt"] is None

    seen = farmer.post(f"{API}/me/tour")
    assert seen.status_code == 200 and seen.json()["tourSeenAt"]
    first = seen.json()["tourSeenAt"]
    # Another phone reads the same account.
    assert farmer.get(f"{API}/me/settings").json()["tourSeenAt"] == first
    # Doing it again keeps the first time.
    assert farmer.post(f"{API}/me/tour").json()["tourSeenAt"] == first

    # Saving settings (even with the field cleared) never brings the tour back.
    settings = farmer.get(f"{API}/me/settings").json()
    saved = farmer.put(f"{API}/me/settings", json={**settings, "language": "en", "tourSeenAt": None})
    assert saved.status_code == 200 and saved.json()["tourSeenAt"] == first
    assert farmer.get(f"{API}/me/settings").json()["tourSeenAt"] == first


def test_marking_the_tour_needs_a_signed_in_farmer():
    from tests.conftest import new_client

    assert new_client().post(f"{API}/me/tour").status_code == 401
