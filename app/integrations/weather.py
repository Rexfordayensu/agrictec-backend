"""Weather from Open-Meteo (https://open-meteo.com): free, no API key, global forecasts. When
Open-Meteo refuses (its free limit is per IP, and hosts such as Render share their IPs, so it
answers 429 by midday) or is down, the backups answer in turn:
1. OpenWeather (https://openweathermap.org), when OPENWEATHER_API_KEY is set: current weather and
   the 5-day forecast in 3-hour steps, with a chance of rain. Its limit is per key, not per IP.
2. MET Norway's Locationforecast (https://api.met.no, free and keyless; CC BY 4.0).

The report has the shape the frontend's weather.service validates (types/market.ts
WeatherReport): the requested location echoed back, current conditions, up to 7 days and the
next 24 hours. MET Norway publishes no chance of rain for Ghana, so with it rainChance is null:
never estimated. Likewise a rain amount or wind speed the provider leaves out is null, never 0.
Answers are cached for 15 minutes per place."""

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.domain.clock import iso
from app.domain.constants import REGIONS

log = logging.getLogger("agrictec.weather")

PROVIDER = "Open-Meteo"
ENDPOINT = "https://api.open-meteo.com/v1/forecast"
MET_PROVIDER = "MET Norway"
MET_ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
OWM_PROVIDER = "OpenWeather"
OWM_BASE = "https://api.openweathermap.org/data/2.5"
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
    """Open-Meteo's answer, or a backup's in the same shape when Open-Meteo can't answer."""
    try:
        return _fetch_open_meteo(lat, lng)
    except WeatherUnavailable:
        pass
    if get_settings().openweather_api_key:
        try:
            raw = owm_to_raw(*_fetch_owm(lat, lng))
            log.warning("Weather: used %s instead of %s.", OWM_PROVIDER, PROVIDER)
            return raw
        except WeatherUnavailable:
            pass
    log.warning("Weather: using %s instead of %s.", MET_PROVIDER, PROVIDER)
    return met_to_raw(_fetch_met(lat, lng))


def _fetch_open_meteo(lat: float, lng: float) -> dict[str, Any]:
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
        except httpx.HTTPStatusError as error:
            # Open-Meteo explains a refusal (e.g. 429, a shared IP over the free daily limit).
            last_error = error
            log.warning("Weather: %s answered %s: %s", PROVIDER, error.response.status_code, error.response.text[:200])
            if error.response.status_code == 429:
                break  # Over the day's limit: asking again won't help.
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
            log.warning("Weather: %s unreachable: %s", PROVIDER, type(error).__name__)
    raise WeatherUnavailable(str(last_error))


def _fetch_met(lat: float, lng: float) -> dict[str, Any]:
    # MET Norway's terms: identify the app in the User-Agent; at most 4 decimals.
    agent = f"AgriCtec/1.0 (+{get_settings().public_app_url})"
    try:
        response = httpx.get(
            MET_ENDPOINT,
            params={"lat": round(lat, 4), "lon": round(lng, 4)},
            headers={"User-Agent": agent},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        log.warning("Weather: %s didn't answer either: %s", MET_PROVIDER, error)
        raise WeatherUnavailable(str(error)) from error


# MET Norway's symbol codes (without _day/_night/_polartwilight) as WMO weather codes.
_MET_SYMBOLS = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65,
    "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightsleet": 66, "sleet": 67, "heavysleet": 67, "lightsleetshowers": 85, "sleetshowers": 85,
    "heavysleetshowers": 86, "lightsnow": 71, "snow": 73, "heavysnow": 75,
    "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
}


def _met_code(symbol: str | None) -> int | None:
    if not symbol:
        return None
    base = symbol.split("_")[0]
    if "thunder" in base:
        return 95
    return _MET_SYMBOLS.get(base)


def _met_period(entry: dict) -> tuple[int, dict] | None:
    """The shortest forecast period an entry carries: (hours, its block)."""
    for hours, name in ((1, "next_1_hours"), (6, "next_6_hours"), (12, "next_12_hours")):
        if name in entry["data"]:
            return hours, entry["data"][name]
    return None


def met_to_raw(met: dict[str, Any]) -> dict[str, Any]:
    """MET Norway's Locationforecast in Open-Meteo's shape (as build_report reads it): hourly
    steps while MET gives them, then 6-hourly; days in GMT (Ghana's time). Wind m/s -> km/h.
    No precipitation probability: MET publishes none for Ghana."""
    try:
        series = met["properties"]["timeseries"]
        first = series[0]
        details = first["data"]["instant"]["details"]
        now_block = first["data"].get("next_1_hours") or first["data"].get("next_6_hours") or {}
        current = {
            # When MET issued the forecast. The first step can be the coming hour (14:00 asked
            # at 13:55), and a time in the future is rejected by the app as an invalid report.
            "time": (met["properties"].get("meta", {}).get("updated_at") or first["time"])[:16],
            "temperature_2m": details["air_temperature"],
            "relative_humidity_2m": details["relative_humidity"],
            "precipitation": (now_block.get("details") or {}).get("precipitation_amount"),
            "weather_code": _met_code((now_block.get("summary") or {}).get("symbol_code")),
            "wind_speed_10m": details["wind_speed"] * 3.6,
        }
        hourly: dict[str, list] = {
            "time": [], "temperature_2m": [], "precipitation_probability": [], "weather_code": [],
        }
        days: dict[str, dict] = {}
        covered_until = ""
        for entry in series:
            moment = entry["time"][:16]
            day = moment[:10]
            instant = entry["data"]["instant"]["details"]
            period = _met_period(entry)
            code = _met_code((period[1].get("summary") or {}).get("symbol_code")) if period else None
            if "next_1_hours" in entry["data"]:
                hourly["time"].append(moment)
                hourly["temperature_2m"].append(instant.get("air_temperature"))
                hourly["precipitation_probability"].append(None)
                hourly["weather_code"].append(code)
            stats = days.setdefault(day, {"temps": [], "codes": [], "rain": 0.0, "rain_known": False, "winds": []})
            if instant.get("air_temperature") is not None:
                stats["temps"].append(instant["air_temperature"])
            if instant.get("wind_speed") is not None:
                stats["winds"].append(instant["wind_speed"] * 3.6)
            six = (entry["data"].get("next_6_hours") or {}).get("details") or {}
            extremes = (six.get("air_temperature_max"), six.get("air_temperature_min"))
            stats["temps"] += [t for t in extremes if t is not None]
            if code is not None:
                stats["codes"].append(code)
            # Rain without counting an hour twice: each step's own period, from where the last ended.
            if period and moment >= covered_until and period[0] <= 6:
                amount = (period[1].get("details") or {}).get("precipitation_amount")
                if amount is not None:
                    stats["rain"] += amount
                    stats["rain_known"] = True
                end = datetime.fromisoformat(moment).replace(tzinfo=UTC).timestamp() + period[0] * 3600
                covered_until = datetime.fromtimestamp(end, UTC).strftime("%Y-%m-%dT%H:%M")
        daily: dict[str, list] = {
            "time": [], "weather_code": [], "temperature_2m_max": [], "temperature_2m_min": [],
            "precipitation_sum": [], "precipitation_probability_max": [], "wind_speed_10m_max": [],
        }
        for day, stats in list(days.items())[:7]:
            if len(stats["temps"]) < 2:
                continue  # A lone reading at midnight isn't a day's range.
            daily["time"].append(day)
            daily["weather_code"].append(max(stats["codes"]) if stats["codes"] else None)
            daily["temperature_2m_max"].append(max(stats["temps"]))
            daily["temperature_2m_min"].append(min(stats["temps"]))
            # None when MET gave no amount for the day (its later days come in 12-hour blocks).
            daily["precipitation_sum"].append(round(stats["rain"], 1) if stats["rain_known"] else None)
            daily["precipitation_probability_max"].append(None)
            daily["wind_speed_10m_max"].append(max(stats["winds"]) if stats["winds"] else None)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise WeatherUnavailable(f"Incomplete answer from {MET_PROVIDER}: {error}") from error
    return {"_provider": MET_PROVIDER, "current": current, "hourly": hourly, "daily": daily}


def _fetch_owm(lat: float, lng: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """OpenWeather's current weather and 5-day forecast. The key travels in the URL, so errors
    are logged by status and OpenWeather's message only, never with the URL."""
    key = get_settings().openweather_api_key
    params = {"lat": round(lat, 4), "lon": round(lng, 4), "units": "metric", "appid": key}
    answers = []
    for path in ("weather", "forecast"):
        try:
            response = httpx.get(f"{OWM_BASE}/{path}", params=params, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            log.warning("Weather: %s unreachable: %s", OWM_PROVIDER, type(error).__name__)
            raise WeatherUnavailable(f"{OWM_PROVIDER} unreachable") from None
        if response.status_code != 200:
            # 401 is a wrong key, or a new one OpenWeather hasn't switched on yet (up to 2 hours).
            log.warning("Weather: %s answered %s: %s", OWM_PROVIDER, response.status_code, response.text[:200])
            raise WeatherUnavailable(f"{OWM_PROVIDER} answered {response.status_code}")
        try:
            answers.append(response.json())
        except ValueError:
            raise WeatherUnavailable(f"{OWM_PROVIDER} answered something unreadable") from None
    return answers[0], answers[1]


def _owm_code(condition: int | None) -> int | None:
    """OpenWeather's condition ID (https://openweathermap.org/weather-conditions) as a WMO code."""
    if condition is None:
        return None
    if 200 <= condition < 300:
        return 95
    if 300 <= condition < 400:
        return {300: 51, 301: 53, 302: 55}.get(condition, 53)
    rain = {500: 61, 501: 63, 502: 65, 503: 65, 504: 65, 511: 66, 520: 80, 521: 81, 522: 82, 531: 82}
    if condition in rain:
        return rain[condition]
    if 600 <= condition < 700:
        return 73
    if condition in (701, 741):
        return 45
    clouds = {800: 0, 801: 1, 802: 2}
    # Haze, dust and smoke (harmattan) keep OpenWeather's own words; their code is "overcast".
    return clouds.get(condition, 3)


def _owm_words(entry: dict) -> tuple[int | None, str | None]:
    weather = (entry.get("weather") or [{}])[0]
    words = (weather.get("description") or "").strip()
    return _owm_code(weather.get("id")), (words[:1].upper() + words[1:]) or None


def _gmt(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M")


def owm_to_raw(now: dict[str, Any], forecast: dict[str, Any]) -> dict[str, Any]:
    """OpenWeather's answers in Open-Meteo's shape (as build_report reads it), with its own
    descriptions. Steps are 3-hourly: the next 24 hours of them, and days in GMT (Ghana's time).
    The chance of rain is OpenWeather's `pop` (0-1) as a percentage. Wind m/s -> km/h."""
    try:
        code, words = _owm_words(now)
        current = {
            "time": _gmt(now["dt"]),  # When it was observed: never in the future.
            "temperature_2m": now["main"]["temp"],
            "relative_humidity_2m": now["main"]["humidity"],
            # OpenWeather leaves "rain" out when it isn't raining (documented): that is 0 mm.
            "precipitation": (now.get("rain") or {}).get("1h", 0.0),
            "weather_code": code,
            "description": words,
            "wind_speed_10m": _kph((now.get("wind") or {}).get("speed")),
        }
        steps = forecast["list"]
        first = steps[0]["dt"]
        hourly: dict[str, list] = {
            "time": [], "temperature_2m": [], "precipitation_probability": [], "weather_code": [], "description": [],
        }
        days: dict[str, dict] = {}
        for step in steps:
            code, words = _owm_words(step)
            chance = step.get("pop")
            moment = _gmt(step["dt"])
            if step["dt"] < first + 24 * 3600:
                hourly["time"].append(moment)
                hourly["temperature_2m"].append(step["main"]["temp"])
                hourly["precipitation_probability"].append(None if chance is None else chance * 100)
                hourly["weather_code"].append(code)
                hourly["description"].append(words)
            day = days.setdefault(
                moment[:10], {"temps": [], "rain": 0.0, "chances": [], "winds": [], "worst": (-1, None, None)}
            )
            main = step["main"]
            day["temps"] += [t for t in (main.get("temp"), main.get("temp_min"), main.get("temp_max")) if t is not None]
            day["rain"] += (step.get("rain") or {}).get("3h", 0.0)
            if chance is not None:
                day["chances"].append(chance * 100)
            wind = _kph((step.get("wind") or {}).get("speed"))
            if wind is not None:
                day["winds"].append(wind)
            if code is not None and code > day["worst"][0]:
                day["worst"] = (code, code, words)
        daily: dict[str, list] = {
            "time": [], "weather_code": [], "description": [], "temperature_2m_max": [], "temperature_2m_min": [],
            "precipitation_sum": [], "precipitation_probability_max": [], "wind_speed_10m_max": [],
        }
        for date, day in list(days.items())[:7]:
            if len(day["temps"]) < 2:
                continue
            daily["time"].append(date)
            daily["weather_code"].append(day["worst"][1])
            daily["description"].append(day["worst"][2])
            daily["temperature_2m_max"].append(max(day["temps"]))
            daily["temperature_2m_min"].append(min(day["temps"]))
            daily["precipitation_sum"].append(round(day["rain"], 1))
            daily["precipitation_probability_max"].append(max(day["chances"]) if day["chances"] else None)
            daily["wind_speed_10m_max"].append(max(day["winds"]) if day["winds"] else None)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise WeatherUnavailable(f"Incomplete answer from {OWM_PROVIDER}: {error}") from error
    return {"_provider": OWM_PROVIDER, "current": current, "hourly": hourly, "daily": daily}


def _kph(metres_per_second: Any) -> float | None:
    return None if metres_per_second is None else float(metres_per_second) * 3.6


def _amount(value: Any) -> float | None:
    """A rain amount or wind speed as given, or None when the provider gave none (never 0)."""
    return None if value is None else max(0.0, round(float(value), 1))


def _chance(value: Any) -> int | None:
    """A rain probability as 0-100, or None when the provider didn't give one."""
    return None if value is None else max(0, min(100, round(float(value))))


def build_report(location: dict, raw: dict[str, Any], now: datetime) -> dict:
    """The provider's answer in the app's WeatherReport shape."""
    provider = raw.get("_provider", PROVIDER)
    try:
        current = raw["current"]
        hourly = raw["hourly"]
        daily = raw["daily"]
        # A provider's own words when it gives them (OpenWeather: "Haze"), else the WMO code's.
        hour_words = hourly.get("description") or [None] * len(hourly["time"])
        day_words = daily.get("description") or [None] * len(daily["time"])
        hours = [
            (_utc(t), temp, _chance(chance), words or describe(code))
            for t, temp, chance, code, words in zip(
                hourly["time"], hourly["temperature_2m"], hourly["precipitation_probability"],
                hourly["weather_code"], hour_words, strict=True,
            )
            if temp is not None
        ]
        now_iso = iso(now.replace(minute=0, second=0, microsecond=0))
        upcoming = [h for h in hours if h[0] >= now_iso][:24]
        rain_now = upcoming[0][2] if upcoming else None
        report = {
            "location": location,
            "provider": provider,
            "updatedAt": _utc(current["time"]),
            "current": {
                "temp": round(float(current["temperature_2m"]), 1),
                "description": current.get("description") or describe(current.get("weather_code")),
                "humidity": max(0, min(100, round(float(current["relative_humidity_2m"])))),
                "rainMm": _amount(current.get("precipitation")),
                "rainChance": rain_now,
                "windKph": _amount(current.get("wind_speed_10m")),
            },
            "daily": [
                {
                    "date": day,
                    "description": words or describe(code),
                    "minTemp": round(float(low), 1),
                    "maxTemp": round(float(high), 1),
                    "rainMm": _amount(rain),
                    "rainChance": _chance(chance),
                    "windKph": _amount(wind),
                    # A thunderstorm in the day's forecast (the weather page and alerts use it).
                    "storm": is_storm(code),
                }
                for day, code, words, high, low, rain, chance, wind in zip(
                    daily["time"], daily["weather_code"], day_words, daily["temperature_2m_max"],
                    daily["temperature_2m_min"], daily["precipitation_sum"],
                    daily["precipitation_probability_max"], daily["wind_speed_10m_max"], strict=True,
                )
                if high is not None and low is not None
            ][:16],
            "upcoming": [
                {"time": t, "description": words, "temp": round(float(temp), 1), "rainChance": chance}
                for t, temp, chance, words in upcoming
            ],
            # Not part of WeatherReport: lets the assistant's advice spot a thunderstorm.
            "_storm": is_storm(current.get("weather_code")),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherUnavailable(f"Incomplete answer from {provider}: {error}") from error
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
