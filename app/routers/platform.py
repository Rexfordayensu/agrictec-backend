"""What the farmer app reads from the platform (services/platform.service.ts), with no sign-in:
the settings the console controls, market data, place search and live updates."""

import json
import threading
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.errors import AppError
from app.core.events import bus
from app.core.ratelimit import rate_limit
from app.core.sessions import ADMIN_COOKIE, FARMER_COOKIE, bearer_token, find_session
from app.db import SessionLocal, get_db
from app.domain.clock import iso, now_utc
from app.domain.market import published_prices
from app.integrations import paystack
from app.integrations import weather as weather_api
from app.schemas.platform import CreditConfig, FeatureFlags, Lender, Plan
from app.schemas.products import LoanProduct
from app.services import account, lending, loan_products, platform_store, subscriptions

router = APIRouter(tags=["Platform"])


def _payments_mode() -> str:
    settings = get_settings()
    if settings.payments_live:
        return "paystack"
    return "demo" if settings.demo_data_allowed else "off"
Database = Annotated[DB, Depends(get_db)]


# HEAD too: uptime monitors (UptimeRobot) check with HEAD by default.
@router.api_route("/health", methods=["GET", "HEAD"], tags=["System"])
def health(db: Database):
    db.execute(text("select 1"))
    return {"status": "ok", "time": iso(now_utc())}


@router.get("/platform")
def snapshot(db: Database):
    """Every platform setting the farmer app reads, in one call. Only published, reviewed market
    prices are included."""
    return {
        "creditConfig": platform_store.credit_config(db),
        "flags": platform_store.flags(db),
        "plans": platform_store.plans(db),
        # Only listed lenders: published, licence verified recently, listing authorised. Never
        # placeholders: an empty list means no lender is available yet.
        "lenders": lending.listed_lenders(db),
        # Published products of listed lenders, latest version (the same for every farmer).
        "loanProducts": [p.model_dump(mode="json", by_alias=True) for p in loan_products.listed_products(db)],
        "marketPrices": published_prices(platform_store.market_prices(db)),
        "crops": platform_store.crops(db),
        "knowledge": platform_store.assistant_kb(db),
        "diagnoses": [d for d in platform_store.diagnoses(db) if d.active],
        "announcements": account.live_announcements(db),
        # "paystack": Paystack (test or live key); "demo": simulated, never outside development;
        # "off": no Paystack key on the live platform, so no paid plan can be bought yet.
        "payments": {"mode": _payments_mode()},
        # "claude" when the assistant and crop doctor use Claude, "off" for the built-in rules.
        "ai": {"mode": "claude" if get_settings().ai_enabled else "off"},
    }


@router.post("/payments/paystack/webhook", include_in_schema=False)
async def paystack_webhook(request: Request):
    """Paystack's notice of a payment. Only a correctly signed notice is acted on, and the
    payment is verified with Paystack again before any plan changes."""
    raw = await request.body()
    if not paystack.valid_signature(raw, request.headers.get("x-paystack-signature")):
        raise AppError("Invalid signature.", status_code=401)
    try:
        event = json.loads(raw)
    except ValueError as error:
        raise AppError("Invalid body.") from error
    reference = (event.get("data") or {}).get("reference")
    if event.get("event") == "charge.success" and reference:
        await run_in_threadpool(_confirm_payment, reference)
    return {"received": True}


def _confirm_payment(reference: str) -> None:
    with SessionLocal() as db:
        try:
            subscriptions.confirm(db, reference)
        except AppError as error:
            if error.status_code != 404:  # Not ours (another app on the same Paystack account).
                raise


@router.get("/platform/flags", response_model=FeatureFlags)
def flags(db: Database):
    return platform_store.flags(db)


@router.get("/platform/credit-config", response_model=CreditConfig)
def credit_config(db: Database):
    """The live credit rules (the farmer app's score and loan checks use them)."""
    return platform_store.credit_config(db)


@router.get("/platform/plans", response_model=list[Plan])
def plans(db: Database):
    return platform_store.plans(db)


@router.get("/platform/lenders", response_model=list[Lender])
def lenders(db: Database):
    """The marketplace: the same published, verified lenders for every farmer."""
    return lending.listed_lenders(db)


@router.get("/platform/lenders/{lender_id}", response_model=Lender)
def lender(lender_id: str, db: Database):
    return lending.listed_lender(db, lender_id)


@router.get("/platform/loan-products", response_model=list[LoanProduct])
def listed_products(db: Database):
    """Published products of listed lenders, each at its latest published version."""
    return loan_products.listed_products(db)


@router.get("/platform/loan-products/{product_id}", response_model=LoanProduct)
def listed_product(product_id: str, db: Database):
    return loan_products.listed_product(db, product_id)


@router.get("/platform/lenders/{lender_id}/logo", include_in_schema=False)
def lender_logo(lender_id: str, db: Database):
    """A listed lender's logo (the URL carries a version, so it can be cached for long)."""
    data, media_type = lending.logo(db, lender_id, listed_only=True)
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/platform/market/prices")
def market_prices(db: Database):
    return published_prices(platform_store.market_prices(db))


@router.get("/platform/market/trends")
def market_trends():
    """Historical charts stay empty until source-backed observations are connected."""
    return {"labels": [], "series": []}


@router.post("/platform/weather", dependencies=[Depends(rate_limit("weather", 60, 60))])
def weather(location: dict):
    """Live weather for a place in Ghana or a map point (Open-Meteo), in the app's WeatherReport
    shape. "unavailable" when the provider can't be reached: nothing is ever guessed."""
    if not weather_api.valid_location(location):
        raise AppError("Select a valid location before requesting weather.")
    try:
        return {"status": "ready", "data": weather_api.public(weather_api.forecast(location))}
    except weather_api.WeatherUnavailable:
        return {
            "status": "unavailable",
            "message": "Weather data unavailable",
            "reason": "The weather service didn't answer just now. Try again in a moment.",
        }


@router.post("/platform/market/research")
def market_research():
    return {
        "status": "unavailable",
        "message": "Market price data unavailable",
        "reason": "An evidence-based market research provider is not connected yet. No recommendation has been generated.",
    }


# ---------- Place search (OpenStreetMap Nominatim) ----------

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_cache: dict[str, list[dict]] = {}
_lock = threading.Lock()
_last_call = [0.0]
_ZOOM = {
    "hamlet": 16, "isolated_dwelling": 16, "farm": 16, "neighbourhood": 16, "locality": 16,
    "village": 15, "suburb": 15, "town": 14, "city": 13, "county": 11, "district": 11,
    "municipality": 11, "state": 9, "region": 9,
}


@router.get("/geocoding/search", dependencies=[Depends(rate_limit("geocoding", 30, 60))])
def search_places(q: Annotated[str, Query(min_length=2, max_length=120)]):
    """Up to 6 places in Ghana. Cached, and at most one upstream call a second (Nominatim's
    usage policy), so browsers no longer call the service directly."""
    key = q.strip().lower()
    if key in _cache:
        return _cache[key]
    with _lock:
        wait = 1.0 - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(
                _NOMINATIM,
                params={"q": q.strip(), "format": "jsonv2", "countrycodes": "gh", "limit": 6, "accept-language": "en"},
                headers={"User-Agent": "AgriCtec/1.0 (farm records platform)", "Accept": "application/json"},
                timeout=8,
            )
            response.raise_for_status()
            items = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AppError("Place search is not available right now.", status_code=502) from error
        finally:
            _last_call[0] = time.monotonic()
    places = []
    for item in items if isinstance(items, list) else []:
        try:
            lat, lng = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        parts = [p.strip() for p in str(item.get("display_name", "")).split(",")]
        name = (item.get("name") or "").strip() or parts[0]
        detail = [p for i, p in enumerate(parts) if not (i == 0 and p == name) and p != "Ghana"][:3]
        places.append(
            {
                "id": str(item.get("place_id")),
                "name": name,
                "detail": ", ".join(detail),
                "lat": lat,
                "lng": lng,
                "zoom": _ZOOM.get(item.get("addresstype") or item.get("type") or "", 14),
            }
        )
    if len(_cache) > 1000:
        _cache.clear()
    _cache[key] = places
    return places



_NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
_reverse_cache: dict[tuple[float, float], dict] = {}


def _reverse_lookup(lat: float, lng: float) -> dict:
    """One Nominatim reverse call (shared lock: at most one upstream call a second)."""
    with _lock:
        wait = 1.0 - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(
                _NOMINATIM_REVERSE,
                params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 16, "addressdetails": 1,
                        "accept-language": "en"},
                headers={"User-Agent": "AgriCtec/1.0 (farm records platform)", "Accept": "application/json"},
                timeout=8,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AppError("Your town could not be found right now.", status_code=502) from error
        finally:
            _last_call[0] = time.monotonic()
    return data if isinstance(data, dict) else {}


@router.get("/geocoding/reverse", dependencies=[Depends(rate_limit("geocoding-reverse", 20, 60))])
def reverse_place(
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
):
    """The town or city and Ghana's region for a position, e.g. {"town": "Ejisu", "region":
    "Ashanti", "label": "Ejisu, Ashanti, Ghana"}. Positions are rounded to about 100 m before the
    lookup (and the cache), so an exact location is never sent on or kept."""
    from app.domain import places

    key = (round(lat, 3), round(lng, 3))
    if key not in _reverse_cache:
        data = _reverse_lookup(*key)
        place = places.describe(data.get("address"))
        place["label"] = places.label(place)
        if len(_reverse_cache) > 2000:
            _reverse_cache.clear()
        _reverse_cache[key] = place
    return _reverse_cache[key]


# ---------- Live updates ----------


@router.get("/events")
def events(request: Request, db: Database):
    """Server-Sent Events: {"topic": "platform"} when the console changes something farmers see,
    {"topic": "account"} when an admin acts on the signed-in farmer. Admins hear every topic."""
    now = now_utc()
    admin = find_session(db, request.cookies.get(ADMIN_COOKIE), ("admin",), now)
    farmer = find_session(db, request.cookies.get(FARMER_COOKIE) or bearer_token(request), ("farmer",), now)
    stream = bus.stream(user_id=farmer.user_id if farmer else None, admin=admin is not None)
    db.close()
    # no-transform: proxies that compress (Next.js, nginx) would otherwise hold events back in
    # their gzip buffer. X-Accel-Buffering: no does the same for nginx's own buffering.
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
