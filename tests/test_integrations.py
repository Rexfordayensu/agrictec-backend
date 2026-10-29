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


# ---------- Market prices (WFP) ----------


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
