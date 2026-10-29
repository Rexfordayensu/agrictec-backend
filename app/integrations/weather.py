"""Weather from Open-Meteo (https://open-meteo.com): free, no API key, global forecasts.

The report has the shape the frontend's weather.service validates (types/market.ts
WeatherReport): the requested location echoed back, current conditions, 7 days and the next
24 hours. Answers are cached for 15 minutes per place."""

import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.domain.clock import iso
from app.domain.constants import REGIONS

PROVIDER = "Open-Meteo"
ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 15 * 60
TIMEOUT_SECONDS = 10

# Where a place-based request is looked up: the city when it's one of these, else the region.
CITY_COORDS = {
    "accra": (5.6037, -0.187),
    "tema": (5.6698, -0.0166),
    "kumasi": (6.6885, -1.6244),
    "tamale": (9.4008, -0.8393),
    "takoradi": (4.8845, -1.7554),
    "cape coast": (5.1053, -1.2466),
    "ho": (6.6008, 0.4713),
    "koforidua": (6.0941, -0.2591),
    "sunyani": (7.3349, -2.3123),
    "wa": (10.0601, -2.5099),
    "bolgatanga": (10.7856, -0.8514),
}
REGION_COORDS = {
    "Ahafo": (6.9, -2.45), "Ashanti": (6.75, -1.6), "Bono": (7.6, -2.55), "Bono East": (7.8, -1.4),
    "Central": (5.55, -1.25), "Eastern": (6.45, -0.55), "Greater Accra": (5.75, -0.1),
    "North East": (10.45, -0.4), "Northern": (9.45, -0.3), "Oti": (8.0, 0.35), "Savannah": (9.1, -1.8),
    "Upper East": (10.8, -0.9), "Upper West": (10.3, -2.3), "Volta": (6.6, 0.45),
    "Western": (5.25, -2.1), "Western North": (6.3, -2.6),
}

# WMO weather interpretation codes (the ones Open-Meteo uses).
_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


class WeatherUnavailable(Exception):
    """The provider didn't answer, or answered something incomplete."""


def describe(code: int | None) -> str:
    return _CODES.get(int(code), "Unsettled") if code is not None else "Unknown"


def is_storm(code: int | None) -> bool:
    return code is not None and int(code) >= 95


def valid_location(location: dict) -> bool:
    """The same rule as the frontend's validWeatherLocation()."""
    label = location.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        return False
    if location.get("kind") == "place":
        city = location.get("city")
        return location.get("region") in REGIONS and (not city or (isinstance(city, str) and len(city) <= 120))
    lat, lng = location.get("latitude"), location.get("longitude")
    return (
        location.get("kind") == "coordinates"
        and isinstance(lat, int | float)
        and isinstance(lng, int | float)
        and abs(lat) <= 90
        and abs(lng) <= 180
    )


def coordinates_of(location: dict) -> tuple[float, float]:
    if location["kind"] == "coordinates":
        return float(location["latitude"]), float(location["longitude"])
    city = (location.get("city") or "").strip().lower()
    return CITY_COORDS.get(city) or REGION_COORDS[location["region"]]


def _utc(local: str) -> str:
    """Open-Meteo's "2026-10-28T14:00" (asked for in GMT) as "2026-10-28T14:00:00.000Z"."""
    return iso(datetime.fromisoformat(local).replace(tzinfo=UTC))


def _fetch(lat: float, lng: float) -> dict[str, Any]:
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lng, 4),
        "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "precipitation_probability_max,wind_speed_10m_max"
        ),
        "timezone": "GMT",
        "forecast_days": 7,
    }
    last_error: Exception | None = None
    for _ in range(2):
        try:
            response = httpx.get(ENDPOINT, params=params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
    raise WeatherUnavailable(str(last_error))


def build_report(location: dict, raw: dict[str, Any], now: datetime) -> dict:
    """The provider's answer in the app's WeatherReport shape."""
    try:
        current = raw["current"]
        hourly = raw["hourly"]
        daily = raw["daily"]
        hours = [
            (_utc(t), temp, chance or 0, code)
            for t, temp, chance, code in zip(
                hourly["time"], hourly["temperature_2m"], hourly["precipitation_probability"],
                hourly["weather_code"], strict=True,
            )
            if temp is not None
        ]
        now_iso = iso(now.replace(minute=0, second=0, microsecond=0))
        upcoming = [h for h in hours if h[0] >= now_iso][:24]
        rain_now = upcoming[0][2] if upcoming else 0
        report = {
            "demo": False,
            "location": location,
            "provider": PROVIDER,
            "updatedAt": _utc(current["time"]),
            "current": {
                "temp": round(float(current["temperature_2m"]), 1),
                "description": describe(current.get("weather_code")),
                "humidity": max(0, min(100, round(float(current["relative_humidity_2m"])))),
                "rainMm": max(0.0, float(current.get("precipitation") or 0)),
                "rainChance": max(0, min(100, round(float(rain_now)))),
                "windKph": max(0.0, round(float(current.get("wind_speed_10m") or 0), 1)),
            },
            "daily": [
                {
                    "date": day,
                    "description": describe(code),
                    "minTemp": round(float(low), 1),
                    "maxTemp": round(float(high), 1),
                    "rainMm": max(0.0, float(rain or 0)),
                    "rainChance": max(0, min(100, round(float(chance or 0)))),
                    "windKph": max(0.0, round(float(wind or 0), 1)),
                }
                for day, code, high, low, rain, chance, wind in zip(
                    daily["time"], daily["weather_code"], daily["temperature_2m_max"],
                    daily["temperature_2m_min"], daily["precipitation_sum"],
                    daily["precipitation_probability_max"], daily["wind_speed_10m_max"], strict=True,
                )
                if high is not None and low is not None
            ][:16],
            "upcoming": [
                {"time": t, "description": describe(code), "temp": round(float(temp), 1), "rainChance": round(float(chance))}
                for t, temp, chance, code in upcoming
            ],
            # Not part of WeatherReport: lets the assistant's advice spot a thunderstorm.
            "_storm": is_storm(current.get("weather_code")),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherUnavailable(f"Incomplete answer from {PROVIDER}: {error}") from error
    return report


def location_key(location: dict) -> str:
    if location["kind"] == "coordinates":
        return f"gps:{float(location['latitude']):.5f}:{float(location['longitude']):.5f}"
    return f"ghana:{location['region']}:{(location.get('city') or '').strip().lower()}"


def forecast(location: dict, now: datetime | None = None, fetch=None) -> dict:
    """The report for a location (cached 15 minutes). Raises WeatherUnavailable."""
    now = now or datetime.now(UTC)
    key = location_key(location)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            raw = cached[1]
        else:
            raw = None
    if raw is None:
        raw = (fetch or _fetch)(*coordinates_of(location))
        with _lock:
            if len(_cache) > 2000:
                _cache.clear()
            _cache[key] = (time.monotonic(), raw)
    return build_report(location, raw, now)


def public(report: dict) -> dict:
    return {k: v for k, v in report.items() if not k.startswith("_")}
