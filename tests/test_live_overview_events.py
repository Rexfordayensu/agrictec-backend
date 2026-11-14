"""The admin overview is live: an open console hears a change event (and re-reads its figures)
when a farmer signs up and when they finish setting up their farm, as it already does for
records, MoMo, verifications and the rest."""

from app.core.events import bus
from tests.conftest import API, ONBOARDING, signup


def test_sign_up_and_farm_setup_tell_open_consoles(monkeypatch):
    heard: list[tuple[str, str | None]] = []
    monkeypatch.setattr(bus, "publish", lambda topic, user_id=None: heard.append((topic, user_id)))

    farmer = signup("live.overview@example.com")
    user_id = farmer.get(f"{API}/auth/session").json()["userId"]
    assert ("account", user_id) in heard, "a new farmer shows on the overview at once"

    heard.clear()
    assert farmer.post(f"{API}/me/onboarding/complete", json=ONBOARDING).status_code == 200
    assert ("account", user_id) in heard, "their region, crops and score show at once"
