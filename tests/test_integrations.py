"""The outside services (weather, market prices, payments, SMS/email, Ghana Card checks), rate
limits and live events. Providers are replaced by recorded answers: no network needed."""

import asyncio
import hashlib
import hmac
import json
import threading
from datetime import UTC, date, datetime

import httpx
import pytest

from app.config import get_settings
from app.core.events import EventBus
from app.domain.assistant import _weather_values
from app.integrations import identity, market_prices, messaging, paystack, weather
from app.services import notifications
from tests.conftest import API, ONBOARDING, new_client, signup, super_admin

# A real Open-Meteo answer for Kumasi (trimmed to two hours and two days).
OPEN_METEO = {
    "current": {"time": "2026-10-28T23:45", "temperature_2m": 24.1, "relative_humidity_2m": 95,
                "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 8.1},
    "hourly": {"time": ["2026-10-29T00:00", "2026-10-29T01:00"], "temperature_2m": [23.9, 23.5],
               "precipitation_probability": [24, 70], "weather_code": [2, 95]},
    "daily": {"time": ["2026-10-28", "2026-10-29"], "weather_code": [80, 81],
              "temperature_2m_max": [31.3, 30.1], "temperature_2m_min": [22.5, 23.0],
              "precipitation_sum": [2.9, 6.7], "precipitation_probability_max": [84, 96],
              "wind_speed_10m_max": [8.4, 10.0]},
}
KUMASI = {"kind": "place", "label": "Kumasi, Ashanti", "region": "Ashanti", "city": "Kumasi"}

# The WFP file's layout: a header, a HXL tag row, then observations.
WFP_CSV = """date,admin1,admin2,market,latitude,longitude,category,commodity,unit,priceflag,pricetype,currency,price,usdprice
#date,#adm1+name,#adm2+name,#loc+market+name,#geo+lat,#geo+lon,#item+type,#item+name,#item+unit,#item+price+flag,#item+price+type,#currency,#value,#value+usd
2026-07-15,Ashanti,Kumasi,Kumasi,6.68,-1.62,cereals and tubers,Maize (white),KG,actual,Retail,GHS,7.5,0.6
2026-08-15,Ashanti,Kumasi,Kumasi,6.68,-1.62,cereals and tubers,Maize (white),KG,actual,Retail,GHS,8.1,0.6
2026-08-15,Ashanti,Ejura,Ejura,7.38,-1.36,cereals and tubers,Maize (white),100 KG,actual,Wholesale,GHS,690,55
2026-08-15,Brong Ahafo,Techiman,Techiman,7.58,-1.94,cereals and tubers,Maize (white),KG,actual,Retail,GHS,6.9,0.5
2026-08-15,Brong Ahafo,Unknownville,Unknownville,7.5,-2.0,cereals and tubers,Maize (white),KG,actual,Retail,GHS,6.0,0.5
2026-08-15,Northern,Tamale,Tamale,9.4,-0.84,cereals and tubers,Rice (local),KG,actual,Retail,GHS,14.0,1.1
2026-08-15,Ashanti,Kumasi,Kumasi,6.68,-1.62,cereals and tubers,Maize (white),Bowl,actual,Retail,GHS,20,1.6
"""


# FPMA's answers on 2026-10-29, trimmed: a series list (other countries included) and prices.
FPMA_SERIES = [
    {"uuid": "t-maize", "iso3_country_code": "GHA", "market_name": "Tamale", "commodity_name": "Maize",
     "price_type": "WHOLESALE", "currency": "GHS", "measure_unit_label": "100 kg"},
    {"uuid": "b-cowpea", "iso3_country_code": "GHA", "market_name": "Bolgatanga", "commodity_name": "Cowpeas (white)",
     "price_type": "WHOLESALE", "currency": "GHS", "measure_unit_label": "109 kg"},
    {"uuid": "t-oil", "iso3_country_code": "GHA", "market_name": "Tamale", "commodity_name": "Palm oil",
     "price_type": "WHOLESALE", "currency": "GHS", "measure_unit_label": "18 Liter"},
    {"uuid": "x-maize", "iso3_country_code": "GHA", "market_name": "Nowhere", "commodity_name": "Maize",
     "price_type": "WHOLESALE", "currency": "GHS", "measure_unit_label": "100 kg"},
    {"uuid": "afg", "iso3_country_code": "AFG", "market_name": "Faryab", "commodity_name": "Maize",
     "price_type": "RETAIL", "currency": "AFN", "measure_unit_label": "Kg"},
]
FPMA_PRICES = [
    {"uuid": "t-maize", "datapoints": [
        {"price_value": 320.51, "date": "2026-05-01", "periodicity": "monthly"},
        {"price_value": 320.51, "date": "2026-04-01", "periodicity": "monthly"},
    ]},
    {"uuid": "b-cowpea", "datapoints": [{"price_value": 1500.0, "date": "2026-05-01", "periodicity": "monthly"}]},
    {"uuid": "t-oil", "datapoints": [{"price_value": 700.0, "date": "2026-05-01", "periodicity": "monthly"}]},
    {"uuid": "x-maize", "datapoints": [{"price_value": 300.0, "date": "2026-05-01", "periodicity": "monthly"}]},
    {"uuid": "afg", "datapoints": [{"price_value": 30.0, "date": "2026-05-01", "periodicity": "monthly"}]},
]


@pytest.fixture
def settings():
    return get_settings()


# ---------- Weather ----------


def test_weather_report_has_the_apps_shape():
    report = weather.build_report(KUMASI, OPEN_METEO, datetime(2026, 10, 29, 0, 20, tzinfo=UTC))
    assert report["location"] == KUMASI and report["provider"] == "Open-Meteo"
    assert report["updatedAt"] == "2026-10-28T23:45:00.000Z"
    assert report["current"] == {"temp": 24.1, "description": "Clear sky", "humidity": 95, "rainMm": 0.0, "rainChance": 24, "windKph": 8.1}
    assert report["daily"][1] == {"date": "2026-10-29", "description": "Showers", "minTemp": 23.0, "maxTemp": 30.1, "rainMm": 6.7, "rainChance": 96, "windKph": 10.0}
    assert report["upcoming"][1]["time"] == "2026-10-29T01:00:00.000Z"
    assert weather.coordinates_of(KUMASI) == weather.CITY_COORDS["kumasi"]


def test_weather_endpoint_and_its_unavailable_answer(monkeypatch):
    monkeypatch.setattr(weather, "_cache", {})
    monkeypatch.setattr(weather, "_fetch", lambda lat, lng: OPEN_METEO)
    r = new_client().post(f"{API}/platform/weather", json=KUMASI).json()
    assert r["status"] == "ready" and r["data"]["current"]["temp"] == 24.1 and "_storm" not in r["data"]

    def down(lat, lng):
        raise weather.WeatherUnavailable("timed out")

    monkeypatch.setattr(weather, "_cache", {})
    monkeypatch.setattr(weather, "_fetch", down)
    assert new_client().post(f"{API}/platform/weather", json=KUMASI).json()["status"] == "unavailable"
    assert new_client().post(f"{API}/platform/weather", json={"kind": "place", "label": "x", "region": "Atlantis"}).status_code == 400


def test_the_assistant_speaks_of_real_weather_only():
    assert "unavailable" in _weather_values(None)["weather"]
    values = _weather_values({"description": "Showers", "temp": 25.0, "humidity": 90, "rainChance": 75, "storm": False})
    assert values["weather"] == "Showers, 25°C, 90% humidity and 75% chance of rain"
    assert "drainage" in values["rainAdvice"]
    # MET Norway gives no probability: the rain amount speaks instead, never a made-up chance.
    values = _weather_values({"description": "Light rain", "temp": 24.0, "humidity": 90, "rainMm": 1.2, "rainChance": None})
    assert values["weather"] == "Light rain, 24°C, 90% humidity and 1.2 mm of rain this hour"
    assert "drainage" in values["rainAdvice"]
    dry = _weather_values({"description": "Cloudy", "temp": 30.0, "humidity": 60, "rainMm": 0.0, "rainChance": None})
    assert "soil moisture" in dry["rainAdvice"]


def _met_step(time, temp, hum, wind, one=None, six=None):
    data = {"instant": {"details": {"air_temperature": temp, "relative_humidity": hum, "wind_speed": wind}}}
    if one:
        data["next_1_hours"] = {"summary": {"symbol_code": one[0]}, "details": {"precipitation_amount": one[1]}}
    if six:
        data["next_6_hours"] = {"summary": {"symbol_code": six[0]}, "details": {"air_temperature_max": six[1], "air_temperature_min": six[2], "precipitation_amount": six[3]}}
    return {"time": time, "data": data}


# MET Norway's Locationforecast for Kumasi (trimmed): hourly steps, then 6-hourly. Issued at
# 21:40; its first step is the coming hour.
MET = {"properties": {"meta": {"updated_at": "2026-10-30T21:40:11Z"}, "timeseries": [
    _met_step("2026-10-30T22:00:00Z", 24.0, 90.0, 2.0, ("rainandthunder", 1.2), ("rain", 24.5, 23.0, 5.0)),
    _met_step("2026-10-30T23:00:00Z", 23.6, 92.0, 1.5, ("cloudy", 0.0), ("rain", 24.0, 22.8, 3.8)),
    _met_step("2026-10-31T00:00:00Z", 23.2, 94.0, 1.0, ("lightrain_night", 0.3), ("lightrain", 23.5, 22.6, 3.0)),
    _met_step("2026-10-31T06:00:00Z", 22.8, 97.0, 1.2, None, ("partlycloudy_day", 29.0, 22.5, 2.0)),
    _met_step("2026-10-31T12:00:00Z", 28.9, 70.0, 3.0, None, ("lightrainshowers_day", 30.2, 26.0, 1.0)),
    _met_step("2026-11-09T00:00:00Z", 23.0, 95.0, 1.0),
]}}


def test_met_norway_answers_in_the_apps_shape_without_a_made_up_chance():
    report = weather.build_report(KUMASI, weather.met_to_raw(MET), datetime(2026, 10, 30, 22, 10, tzinfo=UTC))
    # Updated when MET issued it, never the (possibly future) first step.
    assert report["provider"] == "MET Norway" and report["updatedAt"] == "2026-10-30T21:40:00.000Z"
    assert report["current"] == {"temp": 24.0, "description": "Thunderstorm", "humidity": 90, "rainMm": 1.2, "rainChance": None, "windKph": 7.2}
    assert report["_storm"] is True
    first, second = report["daily"]  # 9 November has one reading only: not a day's range.
    assert (first["date"], first["minTemp"], first["maxTemp"], first["rainMm"], first["rainChance"]) == ("2026-10-30", 22.8, 24.5, 1.2, None)
    # Each hour's rain counted once: 0.3 (00-01h) + 2.0 (06-12h) + 1.0 (12-18h).
    assert (second["date"], second["description"], second["rainMm"]) == ("2026-10-31", "Light showers", 3.3)
    assert [h["rainChance"] for h in report["upcoming"]] == [None, None, None]


def _ts(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def _owm_step(time, temp, low, high, pop, condition, words, wind, rain=None):
    step = {"dt": _ts(time), "main": {"temp": temp, "temp_min": low, "temp_max": high, "humidity": 70},
            "weather": [{"id": condition, "description": words}], "wind": {"speed": wind}, "pop": pop}
    if rain is not None:
        step["rain"] = {"3h": rain}
    return step


# OpenWeather's current weather and 5-day forecast for Tema (trimmed): 3-hour steps.
OWM_NOW = {"dt": _ts("2026-10-30T14:10"), "main": {"temp": 31.3, "humidity": 59},
           "weather": [{"id": 500, "description": "light rain"}], "wind": {"speed": 6.5}, "rain": {"1h": 0.3}}
OWM_FORECAST = {"list": [
    _owm_step("2026-10-30T15:00", 31.0, 30.5, 31.4, 0.62, 500, "light rain", 5.0, rain=0.8),
    _owm_step("2026-10-30T18:00", 28.0, 27.8, 28.3, 0.2, 721, "haze", 4.0),
    _owm_step("2026-10-30T21:00", 26.1, 26.0, 26.4, 0.0, 804, "overcast clouds", 3.0),
    _owm_step("2026-10-31T00:00", 25.2, 25.0, 25.5, 0.1, 800, "clear sky", 2.5),
    _owm_step("2026-10-31T12:00", 31.8, 31.5, 32.0, 0.9, 211, "thunderstorm", 9.0, rain=6.2),
    _owm_step("2026-10-31T15:00", 30.9, 30.7, 31.2, 0.4, 501, "moderate rain", 7.0, rain=2.0),
]}


def test_openweather_answers_with_a_chance_of_rain_and_its_own_words():
    report = weather.build_report(KUMASI, weather.owm_to_raw(OWM_NOW, OWM_FORECAST), datetime(2026, 10, 30, 14, 20, tzinfo=UTC))
    assert report["provider"] == "OpenWeather" and report["updatedAt"] == "2026-10-30T14:10:00.000Z"
    # The chance of rain is the coming 3 hours' probability.
    assert report["current"] == {"temp": 31.3, "description": "Light rain", "humidity": 59, "rainMm": 0.3, "rainChance": 62, "windKph": 23.4}
    # The next 24 hours of steps; harmattan haze keeps its name.
    assert [(h["description"], h["rainChance"]) for h in report["upcoming"]] == [
        ("Light rain", 62), ("Haze", 20), ("Overcast clouds", 0), ("Clear sky", 10), ("Thunderstorm", 90)]
    today, tomorrow = report["daily"]
    assert today == {"date": "2026-10-30", "description": "Light rain", "minTemp": 26.0, "maxTemp": 31.4, "rainMm": 0.8, "rainChance": 62, "windKph": 18.0}
    assert (tomorrow["description"], tomorrow["minTemp"], tomorrow["maxTemp"], tomorrow["rainMm"], tomorrow["rainChance"]) == ("Thunderstorm", 25.0, 32.0, 8.2, 90)


def test_openweather_comes_before_met_norway_and_its_key_stays_out_of_the_logs(settings, monkeypatch, caplog):
    secret = "owm-secret-key-123"
    calls = []
    owm_status = {"code": 200}

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url.split("/")[2] + "/" + url.rsplit("/", 1)[-1])
        request = httpx.Request("GET", url, params=params)
        if "open-meteo" in url:
            return httpx.Response(429, json={"error": True, "reason": "Daily API request limit exceeded."}, request=request)
        if "openweathermap" in url:
            assert params["appid"] == secret and params["units"] == "metric"
            if owm_status["code"] != 200:
                return httpx.Response(owm_status["code"], json={"cod": 401, "message": "Invalid API key."}, request=request)
            return httpx.Response(200, json=OWM_NOW if url.endswith("/weather") else OWM_FORECAST, request=request)
        return httpx.Response(200, json=MET, request=request)

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    monkeypatch.setattr(settings, "openweather_api_key", secret)
    # The test database's migration (alembic's fileConfig) disables existing loggers.
    monkeypatch.setattr(weather.log, "disabled", False)
    assert weather._fetch(5.67, -0.02)["_provider"] == "OpenWeather"
    # A wrong (or not yet active) key: MET Norway answers, and the key isn't logged.
    owm_status["code"] = 401
    calls.clear()
    with caplog.at_level("WARNING"):
        assert weather._fetch(5.67, -0.02)["_provider"] == "MET Norway"
    assert "401" in caplog.text and secret not in caplog.text
    # Without a key, OpenWeather isn't asked at all.
    monkeypatch.setattr(settings, "openweather_api_key", None)
    calls.clear()
    assert weather._fetch(5.67, -0.02)["_provider"] == "MET Norway"
    assert not [c for c in calls if "openweathermap" in c]


def test_met_norway_steps_in_when_open_meteo_refuses(monkeypatch):
    calls = []

    def refused(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if "open-meteo" in url:
            return httpx.Response(429, json={"error": True, "reason": "Daily API request limit exceeded."}, request=httpx.Request("GET", url))
        assert headers["User-Agent"].startswith("AgriCtec/") and params == {"lat": 6.6885, "lon": -1.6244}
        return httpx.Response(200, json=MET, request=httpx.Request("GET", url))

    monkeypatch.setattr(weather.httpx, "get", refused)
    raw = weather._fetch(*weather.coordinates_of(KUMASI))
    assert raw["_provider"] == "MET Norway"
    assert len([u for u in calls if "open-meteo" in u]) == 1  # a 429 isn't asked again
    monkeypatch.setattr(weather.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(weather.WeatherUnavailable):
        weather._fetch(6.0, -1.0)


# ---------- Market prices (MoFA via FAO FPMA, and WFP) ----------


def test_wfp_rows_are_placed_in_todays_regions():
    rows = market_prices.parse_csv(WFP_CSV)
    assert {(o.market, o.region) for o in rows} == {("Kumasi", "Ashanti"), ("Ejura", "Ashanti"), ("Techiman", "Bono East"), ("Tamale", "Northern")}
    bag = next(o for o in rows if o.market == "Ejura")
    assert (bag.unit, bag.quantity, bag.package_kg) == ("bag", 1, 100.0)


def test_the_prices_csv_is_found_in_the_hdx_catalogue():
    # The catalogue's shape as HDX returned it on 2026-10-29: names aren't file names.
    base = "https://data.humdata.org/dataset/626e/resource"
    catalogue = {"result": {"resources": [
        {"format": "CSV", "name": "Ghana - Markets", "url": f"{base}/b/download/wfp_markets_gha.csv"},
        {"format": "CSV", "name": "Ghana - Food Prices", "url": f"{base}/a/download/wfp_food_prices_gha.csv"},
    ]}}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=catalogue)))
    assert market_prices._csv_url(client).endswith("/wfp_food_prices_gha.csv")
    catalogue["result"]["resources"].pop()
    with pytest.raises(market_prices.PricesUnavailable):
        market_prices._csv_url(client)


def test_research_returns_exact_observations_never_an_average():
    rows = market_prices.parse_csv(WFP_CSV)
    query = {"crop": "Maize", "unit": "kg", "quantity": 1, "region": "Ashanti"}
    found = market_prices.research(query, date(2026, 10, 29), rows)
    assert [e["price"] for e in found["evidence"]] == [8.1]  # the latest Kumasi price only
    item = found["evidence"][0]
    assert item["country"] == "GH" and item["currency"] == "GHS" and item["source"]["url"].startswith("https://")
    assert found["recommendedEvidenceId"] == item["id"]
    assert "over a year" not in found["explanation"]
    # The live Ghana dataset currently ends in 2023: old evidence says so.
    assert "over a year ago" in market_prices.research(query, date(2028, 1, 1), rows)["explanation"]
    assert market_prices.research({**query, "region": "Volta"}, date(2026, 10, 29), rows) is None
    bags = market_prices.research({"crop": "Maize", "unit": "bag", "quantity": 1, "packageWeightKg": 100}, date(2026, 10, 29), rows)
    assert bags["evidence"][0]["market"] == "Ejura"


def test_fpma_rows_are_mofa_wholesale_prices_per_package():
    rows = market_prices.parse_fpma(FPMA_SERIES, FPMA_PRICES)
    # Litres aren't comparable, unplaceable markets and other countries are left out.
    assert {(o.market, o.region, o.commodity) for o in rows} == {
        ("Tamale", "Northern", "Maize"), ("Bolgatanga", "Upper East", "Cowpeas (white)")}
    latest = max((o for o in rows if o.market == "Tamale"), key=lambda o: o.day)
    assert (latest.unit, latest.quantity, latest.package_kg, latest.price) == ("bag", 1, 100.0, 320.51)
    assert latest.price_type == "Wholesale" and latest.source == "fpma"


def test_research_takes_the_newest_from_either_source():
    rows = market_prices.parse_csv(WFP_CSV) + market_prices.parse_fpma(FPMA_SERIES, FPMA_PRICES)
    query = {"crop": "Maize", "unit": "bag", "quantity": 1, "packageWeightKg": 100}
    found = market_prices.research(query, date(2026, 10, 29), rows)
    assert [(e["market"], e["observedAt"]) for e in found["evidence"]] == [("Ejura", "2026-08-15"), ("Tamale", "2026-05-01")]
    mofa = found["evidence"][1]
    assert mofa["id"].startswith("fpma-") and mofa["source"]["kind"] == "official"
    assert "Ministry of Food and Agriculture" in mofa["source"]["publisher"]
    cowpeas = market_prices.research({"crop": "Cowpea", "unit": "bag", "quantity": 1, "packageWeightKg": 109}, date(2026, 10, 29), rows)
    assert cowpeas["evidence"][0]["region"] == "Upper East"


def test_one_price_source_down_leaves_the_other(monkeypatch):
    monkeypatch.setattr(market_prices, "_cache", None)

    def wfp_down():
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(market_prices, "_download", wfp_down)
    monkeypatch.setattr(market_prices, "_download_fpma", lambda: (FPMA_SERIES, FPMA_PRICES))
    rows = market_prices.observations()
    assert {o.source for o in rows} == {"fpma"}
    assert market_prices._cache is None  # a partial answer isn't kept for the day
    monkeypatch.setattr(market_prices, "_download_fpma", wfp_down)
    with pytest.raises(market_prices.PricesUnavailable):
        market_prices.observations()


def test_research_endpoint_needs_content_rights(monkeypatch):
    monkeypatch.setattr(market_prices, "observations", lambda: market_prices.parse_csv(WFP_CSV))
    boss = super_admin()
    r = boss.post(f"{API}/admin/content/market/research", json={"crop": "Rice", "unit": "kg", "quantity": 1}).json()
    assert r["status"] == "ready" and r["data"]["evidence"][0]["market"] == "Tamale"
    assert signup().post(f"{API}/admin/content/market/research", json={"crop": "Rice", "unit": "kg", "quantity": 1}).status_code == 401


# ---------- Rate limits ----------


def test_signups_are_rate_limited_per_address():
    client = new_client()
    codes = [client.post(f"{API}/auth/signup", json={"email": f"bot{i}@example.com", "password": "password123"}).status_code for i in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_forwarded_addresses_are_believed_only_from_trusted_networks(settings, monkeypatch):
    from types import SimpleNamespace

    from app.core.ratelimit import client_ip

    def request(peer, forwarded="203.0.113.7, 172.18.0.5"):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": forwarded})

    monkeypatch.setattr(settings, "trusted_proxies", ["127.0.0.1", "172.16.0.0/12"])
    assert client_ip(request("172.18.0.4")) == "203.0.113.7"  # a container on the compose network
    assert client_ip(request("127.0.0.1")) == "203.0.113.7"
    assert client_ip(request("198.51.100.9")) == "198.51.100.9"  # anyone else can't pick an address
    assert client_ip(request("not-an-ip")) == "not-an-ip"
    assert client_ip(request("172.18.0.4", forwarded="")) == "172.18.0.4"
    # A visitor's own made-up entries stay on the left, and each proxy appends the address it saw.
    # The rightmost untrusted entry is believed.
    assert client_ip(request("172.18.0.4", forwarded="1.2.3.4, 203.0.113.7")) == "203.0.113.7"
    assert client_ip(request("172.18.0.4", forwarded="9.9.9.9, 8.8.8.8, 203.0.113.7, 172.18.0.5")) == "203.0.113.7"


def test_a_trusted_proxys_client_ip_header_is_believed_when_configured(settings, monkeypatch):
    from types import SimpleNamespace

    from app.core.ratelimit import client_ip

    def request(peer, headers):
        # Seen in production on Render: Cloudflare's edge address ends X-Forwarded-For, and differs
        # from one request to the next; CF-Connecting-IP holds the visitor's.
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": "1.2.3.4, 41.66.200.7, 104.22.17.40", **headers})

    monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.0/8"])
    stated = {"cf-connecting-ip": "41.66.200.7"}
    assert client_ip(request("10.21.157.68", stated)) == "104.22.17.40"  # not configured: ignored
    monkeypatch.setattr(settings, "trusted_client_ip_header", "cf-connecting-ip")
    assert client_ip(request("10.21.157.68", stated)) == "41.66.200.7"
    assert client_ip(request("10.21.157.68", {"cf-connecting-ip": "junk"})) == "104.22.17.40"
    assert client_ip(request("198.51.100.9", stated)) == "198.51.100.9"  # only from a trusted proxy
    # The app's vouched address still comes first (Vercel's own address is Cloudflare's visitor).
    monkeypatch.setattr(settings, "frontend_proxy_secret", "s3cret-shared-with-the-app")
    vouched = {"x-agrictec-client-ip": "102.176.65.12", "x-agrictec-proxy-secret": "s3cret-shared-with-the-app"}
    assert client_ip(request("10.21.157.68", {**stated, **vouched})) == "102.176.65.12"


def test_the_app_vouches_for_its_visitors_address_only_with_the_shared_secret(settings, monkeypatch):
    from types import SimpleNamespace

    from app.core.ratelimit import client_ip

    def request(headers):
        # Through Vercel, then Render: Render's balancer appends Vercel's own (shared) address.
        return SimpleNamespace(client=SimpleNamespace(host="10.0.0.7"), headers={"x-forwarded-for": "1.2.3.4, 76.76.21.9", **headers})

    monkeypatch.setattr(settings, "trusted_proxies", ["10.0.0.0/8"])
    monkeypatch.setattr(settings, "frontend_proxy_secret", "s3cret-shared-with-the-app")
    vouched = {"x-agrictec-client-ip": "102.176.65.12", "x-agrictec-proxy-secret": "s3cret-shared-with-the-app"}
    assert client_ip(request(vouched)) == "102.176.65.12"
    # A visitor can't vouch for themselves: a wrong or missing secret, or a junk address, is ignored.
    assert client_ip(request({**vouched, "x-agrictec-proxy-secret": "guess"})) == "76.76.21.9"
    assert client_ip(request({"x-agrictec-client-ip": "102.176.65.12"})) == "76.76.21.9"
    assert client_ip(request({**vouched, "x-agrictec-client-ip": "not-an-ip"})) == "76.76.21.9"
    monkeypatch.setattr(settings, "frontend_proxy_secret", None)
    assert client_ip(request(vouched)) == "76.76.21.9"


# ---------- Payments (Paystack) ----------


@pytest.fixture
def paystack_on(settings, monkeypatch):
    monkeypatch.setattr(settings, "paystack_secret_key", "sk_test_123")
    calls = []

    def fake_call(method, path, client=None, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if path == "/transaction/initialize":
            return {"authorization_url": "https://checkout.paystack.com/abc", "reference": kwargs["json"]["reference"]}
        return fake_call.verify_answer

    fake_call.verify_answer = {}
    monkeypatch.setattr(paystack, "_call", fake_call)
    return fake_call, calls


def test_paid_plans_activate_only_once_paystack_confirms_the_full_amount(paystack_on):
    fake, calls = paystack_on
    farmer = signup()
    assert farmer.post(f"{API}/me/subscription", json={"planId": "premium", "method": "card"}).json()["code"] == "payment_required"
    started = farmer.post(f"{API}/me/subscription/checkout", json={"planId": "premium", "method": "mobile-money"}).json()
    assert started["authorizationUrl"] == "https://checkout.paystack.com/abc"
    sent = calls[0][2]
    assert sent["amount"] == 5000 and sent["currency"] == "GHS" and sent["channels"] == ["mobile_money"]

    fake.verify_answer = {"status": "success", "amount": 4000, "currency": "GHS"}  # short-paid
    snap = farmer.post(f"{API}/me/subscription/confirm", json={"reference": started["reference"]}).json()
    assert snap["subscription"]["planId"] == "free"

    fake.verify_answer = {"status": "success", "amount": 5000, "currency": "GHS", "channel": "mobile_money", "authorization": {"brand": "mtn"}}
    snap = farmer.post(f"{API}/me/subscription/confirm", json={"reference": started["reference"]}).json()
    assert snap["subscription"]["planId"] == "premium" and snap["subscription"]["mode"] == "paystack"
    assert snap["subscription"]["payment"]["status"] == "paid" and snap["creditsLimit"] == 100
    # A second confirmation (the webhook) changes nothing.
    again = farmer.post(f"{API}/me/subscription/confirm", json={"reference": started["reference"]}).json()
    assert again["subscription"]["expiresAt"] == snap["subscription"]["expiresAt"]
    assert signup("other@example.com").post(f"{API}/me/subscription/confirm", json={"reference": started["reference"]}).status_code == 404


def test_webhooks_must_be_signed(paystack_on, settings):
    fake, _ = paystack_on
    farmer = signup()
    ref = farmer.post(f"{API}/me/subscription/checkout", json={"planId": "basic", "method": "card"}).json()["reference"]
    body = json.dumps({"event": "charge.success", "data": {"reference": ref}}).encode()
    assert new_client().post(f"{API}/payments/paystack/webhook", content=body, headers={"x-paystack-signature": "forged"}).status_code == 401
    fake.verify_answer = {"status": "success", "amount": 2500, "currency": "GHS"}
    sig = hmac.new(b"sk_test_123", body, hashlib.sha512).hexdigest()
    assert new_client().post(f"{API}/payments/paystack/webhook", content=body, headers={"x-paystack-signature": sig}).status_code == 200
    assert farmer.get(f"{API}/me/subscription").json()["subscription"]["planId"] == "basic"


# ---------- Notifications ----------


def test_admin_decisions_reach_the_farmers_inbox_and_sms(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(messaging, "send_sms", lambda phone, text: sent.append((phone, text)))
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json={**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-123456789-0"}})
    boss = super_admin()
    pending = boss.get(f"{API}/admin/verifications", params={"status": "pending"}).json()[0]
    boss.post(f"{API}/admin/verifications/{pending['id']}/approve")
    inbox = farmer.get(f"{API}/me/notifications").json()
    assert inbox["unread"] == 1 and inbox["items"][0]["title"] == "Your Ghana Card is verified"

    monkeypatch.setattr(settings, "arkesel_api_key", "test-key")
    notifications._send([inbox["items"][0]["id"]])
    assert sent and sent[0][0] == "0241234567" and sent[0][1].startswith("AgriCtec: Your Ghana Card is verified")

    assert farmer.post(f"{API}/me/notifications/read").status_code == 204
    assert farmer.get(f"{API}/me/notifications").json()["unread"] == 0


def test_sms_numbers_and_gateway_errors():
    assert messaging.msisdn("024 123 4567".replace(" ", "")) == "233241234567"
    assert messaging.msisdn("12345") is None
    with pytest.raises(messaging.MessageFailed, match="not configured"):
        messaging.send_sms("0241234567", "hi")


def test_email_uses_starttls_on_587_and_tls_from_the_start_on_465(settings, monkeypatch):
    used = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            used.append((type(self).__name__, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            used.append("starttls")

        def login(self, user, password):
            used.append(("login", user))

        def send_message(self, message):
            used.append(("to", message["To"]))

    class FakeSMTP_SSL(FakeSMTP):
        pass

    monkeypatch.setattr(messaging.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(messaging.smtplib, "SMTP_SSL", FakeSMTP_SSL)
    for name, value in {"smtp_host": "mail.example.com", "smtp_from": "AgriCtec <hi@example.com>", "smtp_username": "hi@example.com"}.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "smtp_port", 587)
    messaging.send_email("ama@example.com", "Hello", "Body")
    assert used == [("FakeSMTP", 587), "starttls", ("login", "hi@example.com"), ("to", "ama@example.com")]
    used.clear()
    monkeypatch.setattr(settings, "smtp_port", 465)
    messaging.send_email("ama@example.com", "Hello", "Body")
    assert used == [("FakeSMTP_SSL", 465), ("login", "hi@example.com"), ("to", "ama@example.com")]


# ---------- Ghana Card checks (Smile ID) ----------


def test_identity_checks_are_advice_for_reviewers_only(settings, monkeypatch):
    monkeypatch.setattr(settings, "smile_id_partner_id", "1234")
    monkeypatch.setattr(settings, "smile_id_api_key", "secret")
    seen = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ResultCode": "1012", "ResultText": "ID Number Validated", "Actions": {"Names": "Exact Match"}})

    real = identity.check_ghana_card
    monkeypatch.setattr(identity, "check_ghana_card", lambda *a: real(*a, client=httpx.Client(transport=httpx.MockTransport(answer))))
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    mine = farmer.post(f"{API}/me/verifications", json={"kind": "ghanaCard", "value": "GHA-123456789-0"}).json()
    assert mine["identityCheck"] is None  # farmers never see it
    assert seen["id_type"] == "GHANA_CARD" and seen["country"] == "GH" and seen["first_name"] == "Ama"
    assert seen["signature"] == identity.signature("secret", "1234", seen["timestamp"])
    review = super_admin().get(f"{API}/admin/verifications", params={"status": "pending"}).json()[0]
    assert review["identityCheck"]["result"] == "verified" and review["identityCheck"]["names"] == "Exact Match"
    assert "123456789" not in json.dumps(review)


# ---------- Live events across workers ----------


def test_the_events_connection_keeps_the_urls_options(settings, monkeypatch):
    from app.core import events

    seen = {}
    monkeypatch.setattr(events.psycopg2, "connect", lambda **kwargs: seen.update(kwargs) or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(settings, "database_url", "postgresql://postgres.ref:pw@pooler.example.com:5432/postgres?sslmode=require")
    with pytest.raises(RuntimeError):
        events._connect()
    assert seen["host"] == "pooler.example.com" and seen["user"] == "postgres.ref" and seen["sslmode"] == "require"


def test_events_reach_streams_on_every_worker():
    """Two buses stand for two API workers: what one publishes, the other's stream receives."""
    received = []

    async def run():
        loop = asyncio.get_running_loop()
        worker_a, worker_b = EventBus(), EventBus()
        worker_a.start(loop)
        worker_b.start(loop)
        stream = worker_b.stream(user_id="farmer-1", admin=False)
        assert await stream.__anext__() == ": connected\n\n"
        await asyncio.sleep(1.5)  # both are LISTENing
        await loop.run_in_executor(None, worker_a.publish, "account", "farmer-1")
        await loop.run_in_executor(None, worker_a.publish, "account", "someone-else")
        received.append(await asyncio.wait_for(stream.__anext__(), 10))
        await stream.aclose()
        for bus in (worker_a, worker_b):
            await loop.run_in_executor(None, bus.stop)

    thread = threading.Thread(target=lambda: asyncio.run(run()))
    thread.start()
    thread.join(30)
    assert received and '"userId": "farmer-1"' in received[0]


# ---------- Deployment ----------


def test_production_needs_the_owners_admin_sign_in():
    from app.config import Settings

    secure = {"environment": "production", "secret_key": "s" * 40, "cookie_secure": True, "seed_demo_data": False}
    with pytest.raises(ValueError, match="ADMIN_EMAIL"):
        Settings(**secure, admin_email="", admin_password="")
    assert Settings(**secure, admin_email="owner@example.com", admin_password="Owner$ecret2026").admin_email
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings(trusted_proxies="127.0.0.1,web")


def test_uptime_monitors_can_check_health_with_head(client):
    assert client.get(f"{API}/health").json()["status"] == "ok"
    head = client.head(f"{API}/health")
    assert head.status_code == 200 and head.content == b""


def test_workers_starting_together_bootstrap_once():
    from sqlalchemy import func, select, text

    from app.db import Base, SessionLocal, engine
    from app.domain.defaults import DEFAULT_LENDERS
    from app.models import Admin, CreditConfigVersion, Lender
    from app.services.backup import bootstrap

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate"))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))
    errors = []
    start = threading.Barrier(4)

    def worker():
        try:
            with SessionLocal() as db:
                start.wait()
                bootstrap(db, seed_demo_data=False)
        except Exception as error:  # noqa: BLE001 - collected for the assertion below
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(CreditConfigVersion)) == 1
        assert db.scalar(select(func.count()).select_from(Lender)) == len(DEFAULT_LENDERS)
        assert db.scalar(select(func.count()).select_from(Admin)) == 1
