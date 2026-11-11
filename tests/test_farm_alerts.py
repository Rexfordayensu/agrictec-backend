"""Weather and market price alerts: the rules, who gets them, and that each goes out once."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db import SessionLocal
from app.services import farm_alerts
from tests.conftest import API, ONBOARDING, signup, super_admin

TODAY = date(2026, 6, 10)  # A June day: growing season in the south and the north.


def day_rows(rain: list[float], chance: list[int | None], storms: list[bool]) -> list[dict]:
    return [
        {"date": (TODAY + timedelta(days=i)).isoformat(), "rainMm": r, "rainChance": c, "storm": s}
        for i, (r, c, s) in enumerate(zip(rain, chance, storms, strict=True))
    ]


def report(rain, chance, storms=None) -> dict:
    return {"daily": day_rows(rain, chance, storms or [False] * len(rain))}


# ---------- The weather rules ----------


def test_heavy_rain_in_the_next_three_days_alerts_once_for_the_nearest_day():
    alerts = farm_alerts.weather_alerts(
        report([2, 25, 40, 0, 0, 0, 0], [20, 80, 90, 10, 10, 10, 10]), "Ashanti", TODAY
    )
    assert [a.title for a in alerts] == ["Heavy rain expected tomorrow"]
    assert alerts[0].key == "weather:wet:2026-06-11"
    assert "about 25 mm" in alerts[0].body and "80% chance" in alerts[0].body


def test_unlikely_or_light_rain_and_rain_beyond_three_days_never_alert():
    unlikely = report([25, 5, 5, 5, 5, 5, 5], [30, 50, 50, 50, 50, 50, 50])
    assert farm_alerts.weather_alerts(unlikely, "Ashanti", TODAY) == []
    assert farm_alerts.weather_alerts(report([0, 0, 0, 45, 0, 5, 5], [50] * 7), "Ashanti", TODAY) == []


def test_a_storm_alerts_and_a_missing_chance_does_not_block_heavy_rain():
    storm = farm_alerts.weather_alerts(report([3, 0, 0, 5, 5, 5, 5], [40] * 7, [True] + [False] * 6), "Ashanti", TODAY)
    assert storm[0].title == "Thunderstorm expected today"
    # MET Norway publishes no chance of rain for Ghana: the amount alone decides.
    no_chance = farm_alerts.weather_alerts(report([0, 0, 30, 5, 5, 5, 5], [None] * 7), "Ashanti", TODAY)
    assert no_chance[0].title.startswith("Heavy rain expected on")


def test_dry_spells_alert_only_in_the_growing_season():
    dry = report([0, 0, 0.5, 0, 0, 0.5, 0], [5] * 7)
    assert [a.title for a in farm_alerts.weather_alerts(dry, "Ashanti", TODAY)] == ["Dry spell ahead"]
    # January is the dry season everywhere; March is still dry in the north.
    assert farm_alerts.weather_alerts(dry, "Ashanti", date(2026, 1, 12)) == []
    assert farm_alerts.weather_alerts(dry, "Upper East", date(2026, 3, 9)) == []
    assert farm_alerts.weather_alerts(dry, "Volta", date(2026, 3, 9)) != []
    # A likely shower anywhere in the week is not a dry spell.
    assert farm_alerts.weather_alerts(report([0, 0, 0, 0, 1, 0, 0], [5, 5, 5, 5, 60, 5, 5]), "Ashanti", TODAY) == []


# ---------- The weather run ----------


def fake_forecast(rain_tomorrow: float):
    """Open-Meteo's raw answer: 7 days, with `rain_tomorrow` mm tomorrow."""

    def fetch(lat, lng):
        days = [(TODAY + timedelta(days=i)).isoformat() for i in range(7)]
        return {
            "current": {"time": f"{TODAY}T06:00", "temperature_2m": 26, "relative_humidity_2m": 80,
                        "precipitation": 0, "weather_code": 3, "wind_speed_10m": 8},
            "hourly": {"time": [], "temperature_2m": [], "precipitation_probability": [], "weather_code": []},
            "daily": {
                "time": days, "weather_code": [3] * 7, "temperature_2m_max": [30] * 7,
                "temperature_2m_min": [22] * 7, "precipitation_sum": [0, rain_tomorrow, 0, 3, 3, 3, 3],
                "precipitation_probability_max": [10, 85, 10, 40, 40, 40, 40], "wind_speed_10m_max": [10] * 7,
            },
        }

    return fetch


@pytest.fixture(autouse=True)
def fresh_forecasts():
    """The weather module caches forecasts for 15 minutes; each test brings its own."""
    farm_alerts.weather_api._cache.clear()
    yield
    farm_alerts.weather_api._cache.clear()


def inbox(client) -> list[dict]:
    return client.get(f"{API}/me/notifications").json()["items"]


def test_the_weather_run_alerts_subscribed_farmers_once():
    ama = signup("ama@example.com")
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    kofi = signup("kofi@example.com")
    kofi.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    no_weather = {"aiInsights": True, "weatherAlerts": False, "priceAlerts": True}
    kofi.put(f"{API}/me/settings", json={"language": "en", "notifications": no_weather})
    now = datetime(2026, 6, 10, 6, 0, tzinfo=UTC)

    with SessionLocal() as db:
        assert farm_alerts.run_weather_alerts(db, now, force=True, fetch=fake_forecast(32)) == 1
        # The same forecast again: nothing new to say.
        assert farm_alerts.run_weather_alerts(db, now, force=True, fetch=fake_forecast(32)) == 0
    weather = [n for n in inbox(ama) if n["kind"] == "weather"]
    assert [n["title"] for n in weather] == ["Heavy rain expected tomorrow"] and weather[0]["href"] == "/weather"
    assert not [n for n in inbox(kofi) if n["kind"] == "weather"]  # switched off


def test_the_run_happens_once_per_slot_and_no_forecast_means_no_alert():
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    now = datetime(2026, 6, 10, 7, 30, tzinfo=UTC)

    def down(lat, lng):
        raise farm_alerts.weather_api.WeatherUnavailable("down")

    with SessionLocal() as db:
        assert farm_alerts.run_weather_alerts(db, now, fetch=down) == 0  # claims the 06:00 slot
        assert farm_alerts.run_weather_alerts(db, now, fetch=fake_forecast(32)) == 0  # already done
        assert farm_alerts.run_weather_alerts(db, now.replace(hour=16, minute=5), fetch=fake_forecast(32)) == 1
    assert not [n for n in inbox(farmer) if "unavailable" in n["body"].lower()]


# ---------- Market prices ----------


def observation(pid: str, price: float, observed: str, region: str = "Ashanti", crop: str = "Maize (white)") -> dict:
    return {
        "id": pid, "country": "GH", "currency": "GHS", "crop": crop, "unit": "bag", "quantity": 1,
        "packageWeightKg": 100, "price": price, "change": 0, "status": "published", "scope": "region",
        "region": region, "observedAt": observed,
        "source": {"title": "Weekly prices", "publisher": "MoFA SRID", "url": "https://mofa.gov.gh/prices",
                   "publishedAt": observed, "kind": "official"},
        "reviewedAt": "2026-10-20T10:00:00.000Z", "reviewedBy": "Super Admin",
    }


def test_a_published_price_move_of_ten_percent_reaches_growers_in_that_region():
    boss = super_admin()
    ashanti = signup("ama@example.com")
    ashanti.post(f"{API}/me/onboarding/complete", json=ONBOARDING)  # Ashanti, grows Maize
    volta = signup("kofi@example.com")
    volta.post(f"{API}/me/onboarding/complete", json={"profile": {**ONBOARDING["profile"], "region": "Volta",
                                                                  "constituency": "Ho Central", "gps": None}})
    first = [observation("maize-1", 400, "2026-10-01")]
    assert boss.put(f"{API}/admin/content/market", json=first).status_code == 200, "first price"
    assert not [n for n in inbox(ashanti) if n["kind"] == "price"]  # nothing to compare with yet

    small = [*first, observation("maize-2", 420, "2026-10-08")]  # +5%: not news
    assert boss.put(f"{API}/admin/content/market", json=small).status_code == 200
    assert not [n for n in inbox(ashanti) if n["kind"] == "price"]

    jump = [*small, observation("maize-3", 480, "2026-10-15")]  # +14% on the previous one
    assert boss.put(f"{API}/admin/content/market", json=jump).status_code == 200
    prices = [n for n in inbox(ashanti) if n["kind"] == "price"]
    assert [n["title"] for n in prices] == ["Maize (white) price up 14%"] and "Ashanti" in prices[0]["body"]
    assert not [n for n in inbox(volta) if n["kind"] == "price"]  # a regional price, another region
    # Saving the same list again sends nothing new.
    boss.put(f"{API}/admin/content/market", json=jump)
    assert len([n for n in inbox(ashanti) if n["kind"] == "price"]) == 1


# ---------- Nothing made up ----------


def test_missing_rain_or_wind_stays_unknown_and_never_alerts_as_dry():
    from app.integrations import weather

    raw = fake_forecast(0)(0, 0)
    raw["current"]["precipitation"] = None
    raw["current"]["wind_speed_10m"] = None
    raw["daily"]["precipitation_sum"][2] = None
    raw["daily"]["wind_speed_10m_max"][2] = None
    report = weather.build_report({"kind": "place", "label": "Ashanti", "region": "Ashanti"}, raw,
                                  datetime(2026, 6, 10, 6, tzinfo=UTC))
    assert report["current"]["rainMm"] is None and report["current"]["windKph"] is None
    assert report["daily"][2]["rainMm"] is None and report["daily"][2]["windKph"] is None
    assert "demo" not in report
    # Six known dry days and one unknown: that day could be the rain, so no dry spell.
    dry_but_one = {"daily": [{**d, "rainMm": 0.0, "rainChance": 5} for d in report["daily"]]}
    dry_but_one["daily"][2]["rainMm"] = None
    assert farm_alerts.weather_alerts(dry_but_one, "Ashanti", TODAY) == []
    # An unknown MET Norway symbol is unknown, not "cloudy".
    assert weather._met_code("someNewSymbol_day") is None and weather.describe(None) == "Unknown"


def test_admins_cannot_publish_made_up_weather(client):
    boss = super_admin()
    fake = {"condition": "sunny", "temp": 30, "description": "Sunny", "humidity": 50, "rainChance": 0}
    assert boss.put(f"{API}/admin/content/weather-override", json=fake).status_code in (404, 405)
    assert "weatherOverride" not in client.get(f"{API}/platform").json()
