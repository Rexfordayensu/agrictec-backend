"""Naming a GPS position: village, town or city, district and Ghana's region (GET /geocoding/reverse).
The addresses below are OpenStreetMap Nominatim's real answers (checked 2026-11-13)."""

from app.domain import places
from app.routers import platform
from tests.conftest import API, new_client


def test_regions_are_named_as_the_app_names_them():
    assert places.region_name("Ashanti Region") == "Ashanti"
    assert places.region_name("Greater Accra Region") == "Greater Accra"
    assert places.region_name("bono east region") == "Bono East"
    assert places.region_name("Western North") == "Western North"
    # Not one of Ghana's 16 regions: left out, never guessed.
    assert places.region_name("Centre-Est") is None
    assert places.region_name(None) is None


def test_a_village_keeps_its_district_and_no_town_is_made_up():
    krapa = places.describe({"village": "Krapa", "county": "Ejisu Municipal District",
                             "state": "Ashanti Region", "country_code": "gh"})
    assert krapa == {"village": "Krapa", "area": None, "town": None, "district": "Ejisu Municipal District",
                     "region": "Ashanti", "inGhana": True}
    assert places.label(krapa) == "Krapa, Ashanti, Ghana"
    asueyi = places.describe({"village": "Asueyi", "county": "Techiman North District",
                              "state": "Bono East Region", "country_code": "gh"})
    assert (asueyi["village"], asueyi["district"], asueyi["region"]) == ("Asueyi", "Techiman North District", "Bono East")


def test_a_city_names_its_neighbourhood_and_the_city():
    kumasi = places.describe({"suburb": "Adum", "city": "Kumasi", "county": "Kumasi Metropolitan District",
                              "state": "Ashanti Region", "country_code": "gh"})
    assert (kumasi["village"], kumasi["area"], kumasi["town"], kumasi["region"]) == (None, "Adum", "Kumasi", "Ashanti")
    assert places.label(kumasi) == "Kumasi, Ashanti, Ghana"
    tamale = places.describe({"suburb": "Tamale Aboabo", "city": "Tamale", "state": "Northern Region",
                              "country_code": "gh"})
    assert places.label(tamale) == "Tamale, Northern, Ghana" and tamale["area"] == "Tamale Aboabo"
    # Only a district: the district names it.
    rural = places.describe({"county": "Sene West District", "state": "Bono East Region", "country_code": "gh"})
    assert places.label(rural) == "Sene West District, Bono East, Ghana"


def test_outside_ghana_says_so():
    lome = places.describe({"city": "Lomé", "state": "Maritime", "country_code": "tg"})
    assert lome["region"] is None and lome["inGhana"] is False
    assert places.label(lome) == "Lomé (outside Ghana)"
    assert places.label(places.describe({})) == "Your current location (outside Ghana)"


def test_the_endpoint_rounds_the_position_and_caches_it(monkeypatch):
    calls = []

    def fake(lat, lng):
        calls.append((lat, lng))
        return {"address": {"village": "Krapa", "county": "Ejisu Municipal District", "state": "Ashanti Region",
                            "country_code": "gh"}}

    monkeypatch.setattr(platform, "_reverse_lookup", fake)
    platform._reverse_cache.clear()
    client = new_client()
    first = client.get(f"{API}/geocoding/reverse", params={"lat": 6.718634, "lng": -1.486721})
    assert first.status_code == 200
    assert first.json() == {"village": "Krapa", "area": None, "town": None, "district": "Ejisu Municipal District",
                            "region": "Ashanti", "inGhana": True, "label": "Krapa, Ashanti, Ghana"}
    # Rounded to about 100 m before it leaves the server, and asked only once.
    again = client.get(f"{API}/geocoding/reverse", params={"lat": 6.7186, "lng": -1.4672 - 0.0195})
    assert again.json()["village"] == "Krapa"
    assert calls == [(6.719, -1.487)]
    assert client.get(f"{API}/geocoding/reverse", params={"lat": 95, "lng": 0}).status_code == 422
