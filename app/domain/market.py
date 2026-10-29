"""Market price publication rules (lib/market/evidence.ts). A price reaches farmers only when it
is a reviewed, source-backed Ghanaian observation."""

from datetime import date

from pydantic import ValidationError

from app.domain.constants import REGIONS
from app.schemas.platform import MarketPrice, PublishedSource

MARKET_UNITS = ["kg", "bag", "crate", "tonne"]


def publication_error(price: MarketPrice, today: date | None = None) -> str | None:
    """Why this observation can't be published (None when it can)."""
    if not price.id or not price.scope or not price.source or not price.observed_at:
        return "Add the observation date, coverage and source before publishing."
    try:
        source = PublishedSource.model_validate(price.source.model_dump())
    except ValidationError:
        return "Add a valid source title, publisher, HTTP/HTTPS link and publication date."
    if not str(source.url).startswith(("http://", "https://")):
        return "Add a valid source title, publisher, HTTP/HTTPS link and publication date."
    if price.country != "GH" or price.currency != "GHS":
        return "Only Ghanaian observations explicitly priced in GHS can be published."
    if price.unit not in MARKET_UNITS or not price.quantity or price.price <= 0:
        return "Choose a standard unit, quantity and a price greater than zero."
    if price.price_max is not None and price.price_max < price.price:
        return "The upper price cannot be below the lower price."
    if price.scope != "national" and (price.region or "") not in REGIONS:
        return "Select the Ghanaian region covered by this observation."
    if price.scope == "market" and not (price.market or "").strip():
        return "Name the market covered by this observation."
    if price.scope == "national" and not price.national_coverage_confirmed:
        return "Confirm the source explicitly supports nationwide coverage."
    if today and (price.observed_at > today or source.published_at > today):
        return "Prices and sources cannot be dated in the future."
    if not price.reviewed_at or not price.reviewed_by:
        return "An administrator must review the evidence before publication."
    return None


def published_prices(prices: list[MarketPrice]) -> list[MarketPrice]:
    return [p for p in prices if p.status == "published" and publication_error(p) is None]


def unit_label(price: MarketPrice) -> str:
    """"1 bag (50 kg each)"."""
    quantity = price.quantity or 1
    quantity_text = str(int(quantity)) if float(quantity).is_integer() else str(quantity)
    if price.package_weight_kg:
        weight = price.package_weight_kg
        suffix = f" ({int(weight) if float(weight).is_integer() else weight} kg each)"
    elif price.unit in ("bag", "crate"):
        suffix = " (weight not supplied)"
    else:
        suffix = ""
    return f"{quantity_text} {price.unit}{suffix}"


def format_market_amount(amount: float) -> str:
    """Keeps the source's pesewas: "GHS 1,234.5"."""
    text = f"{amount:,.10f}".rstrip("0").rstrip(".")
    return f"GHS {text}"
