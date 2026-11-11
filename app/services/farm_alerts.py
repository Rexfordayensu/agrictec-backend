"""Weather and market price alerts for farmers (Settings > Preferences > Notifications).

Every alert lands in the app's inbox (the bell), and goes by SMS and email when those are set up
(services/notifications.py). Each one is sent once: farm_alerts remembers what went out.

Weather (twice a day, 06:00 and 16:00 Ghana time): the 7-day forecast at the farm's map pin (or
its region when there is no pin), from the same providers as the weather page.
- Heavy rain or a thunderstorm in the next 3 days: at least HEAVY_RAIN_MM of rain in a day with
  a rain chance of RAIN_CHANCE_MIN% or more (or no chance published), or a thunderstorm code.
- A dry spell: under DRY_TOTAL_MM of rain over the next 7 days, every day under DRY_CHANCE_MAX%,
  and only in the growing season for the farm's zone (so the long dry season never alerts).

Market prices (when an admin publishes a reviewed price): a change of PRICE_CHANGE_MIN% or more
against the previous published observation for the same crop, unit and place, sent to farmers
who grow that crop (and, for a market or regional price, who farm in that region). Nothing is
estimated: no forecast, no alert; no earlier price to compare with, no alert."""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session as DB

from app.domain.clock import now_utc
from app.domain.market import format_market_amount, published_prices, unit_label
from app.integrations import weather as weather_api
from app.models import FarmAlert, Profile, User, UserSettings
from app.schemas.platform import MarketPrice
from app.services import notifications, platform_store

log = logging.getLogger("agrictec.farm_alerts")

HEAVY_RAIN_MM = 20.0
RAIN_CHANCE_MIN = 60
ALERT_WINDOW_DAYS = 3
DRY_DAYS = 7
DRY_TOTAL_MM = 2.0
DRY_CHANCE_MAX = 30
PRICE_CHANGE_MIN = 10.0
# Ghana is on UTC all year: alerts go out at 06:00 and 16:00.
RUN_HOURS = (6, 16)
# Forecast requests per run at most (farmers on nearby pins share one).
MAX_LOCATIONS = 400
LAST_RUN_KEY = "alerts:weather:lastSlot"
RUN_LOCK = 4_710_222

NORTHERN_REGIONS = {"Northern", "Savannah", "North East", "Upper East", "Upper West"}
# Months a dry spell harms crops: the north has one rainy season, the south two.
GROWING_MONTHS = {"north": {5, 6, 7, 8, 9, 10}, "south": {3, 4, 5, 6, 7, 9, 10, 11}}


@dataclass(frozen=True)
class Alert:
    key: str
    title: str
    body: str
    href: str


# ---------- Weather: the rules ----------


def _when(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"on {day.strftime('%A')}"


def in_growing_season(region: str, month: int) -> bool:
    zone = "north" if region in NORTHERN_REGIONS else "south"
    return month in GROWING_MONTHS[zone]


def weather_alerts(report: dict, region: str, today: date) -> list[Alert]:
    """What a forecast warns this farm about (at most one wet alert and one dry alert)."""
    daily = report.get("daily") or []
    out: list[Alert] = []

    for day in daily:
        storm = bool(day.get("storm"))
        when = date.fromisoformat(day["date"])
        if when < today or when > today + timedelta(days=ALERT_WINDOW_DAYS - 1):
            continue
        # An amount the provider didn't give is unknown: it never counts as heavy (or as dry).
        rain, chance = day.get("rainMm"), day.get("rainChance")
        heavy = rain is not None and rain >= HEAVY_RAIN_MM and (chance is None or chance >= RAIN_CHANCE_MIN)
        if not (heavy or storm):
            continue
        likely = f" ({chance}% chance)" if chance is not None else ""
        amount = f"about {rain:.0f} mm of rain{likely}" if rain is not None and rain >= 1 else "thunderstorms"
        title = "Thunderstorm expected" if storm else "Heavy rain expected"
        out.append(
            Alert(
                key=f"weather:wet:{when.isoformat()}",
                title=f"{title} {_when(when, today)}",
                body=(
                    f"The forecast for your farm shows {amount} {_when(when, today)}. Cover or move "
                    "harvested crops, clear drainage channels, and wait until it passes to spray or "
                    "apply fertilizer."
                ),
                href="/weather",
            )
        )
        break  # The nearest wet day is the one to act on.

    week = daily[:DRY_DAYS]
    # A dry spell needs every day's amount: a missing one could be the rain.
    known = all(d.get("rainMm") is not None for d in week)
    if len(week) == DRY_DAYS and known and in_growing_season(region, today.month):
        total = sum(float(d["rainMm"]) for d in week)
        chances_low = all(d.get("rainChance") is None or d["rainChance"] < DRY_CHANCE_MAX for d in week)
        if total < DRY_TOTAL_MM and chances_low:
            year, number, _ = today.isocalendar()
            out.append(
                Alert(
                    key=f"weather:dry:{year}-W{number:02d}",
                    title="Dry spell ahead",
                    body=(
                        f"Almost no rain is forecast at your farm for the next {DRY_DAYS} days "
                        f"({total:.1f} mm in total). Water young plants early or late in the day, "
                        "mulch to keep moisture in, and hold off planting or fertilizer until rain returns."
                    ),
                    href="/weather",
                )
            )
    return out


# ---------- Who gets alerts ----------


def _wants(row: UserSettings | None, name: str) -> bool:
    """On unless the farmer switched it off (stored snake_case; camelCase accepted)."""
    values = (row.notifications if row else None) or {}
    camel = re.sub(r"_(\w)", lambda m: m.group(1).upper(), name)
    return bool(values.get(name, values.get(camel, True)))


def _subscribers(db: DB, setting: str) -> list[tuple[User, Profile]]:
    """Registered, not suspended, and with this alert switched on."""
    rows = db.execute(
        select(User, Profile, UserSettings)
        .join(Profile, Profile.user_id == User.id)
        .outerjoin(UserSettings, UserSettings.user_id == User.id)
        .where(User.is_guest.is_(False), User.status != "suspended")
    ).all()
    return [(user, profile) for user, profile, settings in rows if _wants(settings, setting)]


def _send(db: DB, user_id: str, alert: Alert, kind: str):
    """Records and queues one alert; None when it was already sent. Not committed."""
    if db.scalar(select(FarmAlert.id).where(FarmAlert.user_id == user_id, FarmAlert.key == alert.key)):
        return None
    db.add(FarmAlert(user_id=user_id, key=alert.key, created_at=now_utc()))
    return notifications.create(db, user_id, kind, alert.title, alert.body, alert.href)


# ---------- Weather: the run ----------


def _location(profile: Profile) -> dict | None:
    """The farm's pin (rounded to ~1 km so neighbours share a forecast), else its region."""
    if profile.gps_lat is not None and profile.gps_lng is not None:
        return {
            "kind": "coordinates",
            "label": "Your farm",
            "latitude": round(profile.gps_lat, 2),
            "longitude": round(profile.gps_lng, 2),
        }
    if profile.region in weather_api.REGION_COORDS:
        return {"kind": "place", "label": profile.region, "region": profile.region}
    return None


def _due_slot(now: datetime) -> datetime:
    """The latest 06:00 or 16:00 at or before now."""
    today = now.replace(minute=0, second=0, microsecond=0)
    for hour in sorted(RUN_HOURS, reverse=True):
        slot = today.replace(hour=hour)
        if slot <= now:
            return slot
    return (today - timedelta(days=1)).replace(hour=max(RUN_HOURS))


def _claim(db: DB, now: datetime) -> bool:
    """True for exactly one worker per slot (an advisory lock, then the slot is recorded)."""
    if not db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": RUN_LOCK}).scalar():
        db.rollback()
        return False
    slot = _due_slot(now).isoformat()
    last = platform_store.get_raw(db, LAST_RUN_KEY)
    if last and last >= slot:
        db.rollback()
        return False
    platform_store.put_raw(db, LAST_RUN_KEY, slot, None)
    db.commit()
    return True


def run_weather_alerts(db: DB, now: datetime | None = None, *, force: bool = False, fetch=None) -> int:
    """Checks the forecast for every subscribed farm once per slot. How many alerts went out."""
    now = now or now_utc()
    if not force and not _claim(db, now):
        return 0
    groups: dict[str, tuple[dict, list[Profile]]] = {}
    for _, profile in _subscribers(db, "weather_alerts"):
        location = _location(profile)
        if location:
            key = weather_api.location_key(location)
            groups.setdefault(key, (location, []))[1].append(profile)
    if len(groups) > MAX_LOCATIONS:
        log.warning("Weather alerts: %s farm locations; checking the first %s", len(groups), MAX_LOCATIONS)

    sent = 0
    for location, profiles in list(groups.values())[:MAX_LOCATIONS]:
        try:
            report = weather_api.forecast(location, now, fetch=fetch)
        except weather_api.WeatherUnavailable:
            continue  # No forecast, no alert: nothing is guessed.
        notices = []
        for profile in profiles:
            for alert in weather_alerts(report, profile.region, now.date()):
                notice = _send(db, profile.user_id, alert, "weather")
                if notice is not None:
                    notices.append(notice)
        db.commit()
        notifications.deliver(*notices)
        sent += len(notices)
    if sent:
        log.info("Weather alerts: %s sent", sent)
    return sent


# ---------- Market prices ----------


def _series(price: MarketPrice) -> tuple:
    """Observations of the same thing over time: crop, unit and place."""
    return (
        price.crop.strip().lower(),
        price.unit,
        price.quantity or 1,
        price.package_weight_kg,
        price.scope,
        (price.region or "").strip(),
        (price.market or "").strip().lower(),
    )


def price_changes(before: list[MarketPrice], after: list[MarketPrice]) -> list[tuple[MarketPrice, MarketPrice, float]]:
    """(new, previous, % change) for each newly published price that moved PRICE_CHANGE_MIN% or
    more against the previous published observation of the same series."""
    old = {p.id: p for p in published_prices(before) if p.id}
    now_published = published_prices(after)
    out = []
    for price in now_published:
        same = old.get(price.id) if price.id else None
        if same and same.price == price.price:
            continue  # Already published, unchanged: nothing new.
        if same:
            base = same
        else:
            earlier = [
                q
                for q in [*old.values(), *now_published]
                if q.id != price.id
                and _series(q) == _series(price)
                and q.observed_at
                and price.observed_at
                and q.observed_at < price.observed_at
            ]
            base = max(earlier, key=lambda q: q.observed_at) if earlier else None
        if base is None or base.price <= 0:
            continue
        change = (price.price - base.price) / base.price * 100
        if abs(change) >= PRICE_CHANGE_MIN:
            out.append((price, base, change))
    return out


def _crop_word(name: str) -> str:
    """"Maize (white)" -> "maize": the crop a farmer would list."""
    found = re.match(r"[a-z]+", name.strip().lower())
    return found.group(0) if found else ""


def price_alert(price: MarketPrice, previous: MarketPrice, change: float) -> Alert:
    direction = "up" if change > 0 else "down"
    where = price.market or price.region or "Ghana"
    unit = unit_label(price)
    source = f" Source: {price.source.publisher}." if price.source else ""
    return Alert(
        key=f"price:{price.id or _series(price)}:{price.price}",
        title=f"{price.crop} price {direction} {abs(change):.0f}%",
        body=(
            f"{price.crop} in {where}: {format_market_amount(price.price)} per {unit}, {direction} from "
            f"{format_market_amount(previous.price)}"
            + (f" on {previous.observed_at:%d %b}" if previous.observed_at else "")
            + f".{source}"
        ),
        href="/market",
    )


def send_price_alerts(db: DB, before: list[MarketPrice], after: list[MarketPrice]) -> int:
    """After an admin saves market prices. Never lets a problem here undo the save."""
    try:
        changes = price_changes(before, after)
        if not changes:
            return 0
        farmers = _subscribers(db, "price_alerts")
        notices = []
        for price, previous, change in changes:
            crop = _crop_word(price.crop)
            local = price.scope in ("market", "region") and price.region
            alert = price_alert(price, previous, change)
            for _, profile in farmers:
                grows = any(_crop_word(c) == crop for c in profile.primary_crops or [])
                if grows and (not local or profile.region == price.region):
                    notice = _send(db, profile.user_id, alert, "price")
                    if notice is not None:
                        notices.append(notice)
        db.commit()
        notifications.deliver(*notices)
        return len(notices)
    except Exception:  # The prices are saved already; an alert problem is only logged.
        db.rollback()
        log.warning("Market price alerts failed", exc_info=True)
        return 0
