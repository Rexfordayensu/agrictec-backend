"""Ghana market prices from two free, open sources, both monthly and in GHS:
- MoFA's own wholesale prices (SRID Marketing Services Unit), as FAO's FPMA tool republishes them
  (https://fpma.fao.org/giews/fpmat4/). Accra, Kumasi, Tamale, Techiman, Bolgatanga and Wa; the
  northern and middle-belt markets are the most current.
- The World Food Programme's dataset on HDX
  (https://data.humdata.org/dataset/wfp-food-prices-for-ghana): retail and wholesale prices from
  WFP's market monitoring (CC BY-IGO).

Research returns exact observations only, never an average: the latest price per market for
the asked crop, unit and package size, in a region it can name for certain. The admin still
reviews and publishes each one (lib/market/evidence.ts rules). One source being down leaves the
other's evidence; only both failing makes research unavailable."""

import csv
import io
import re
import threading
import time
from dataclasses import dataclass
from datetime import date

import httpx

from app.domain.constants import REGIONS

DATASET = "wfp-food-prices-for-ghana"
DATASET_PAGE = f"https://data.humdata.org/dataset/{DATASET}"
CATALOGUE = f"https://data.humdata.org/api/3/action/package_show?id={DATASET}"
PUBLISHER = "World Food Programme (WFP), via the Humanitarian Data Exchange"
FPMA_API = "https://fpma.fao.org/giews/v4/global/price_module/api/v1"
FPMA_PAGE = "https://fpma.fao.org/giews/fpmat4/#/dashboard/tool/domestic"
FPMA_PUBLISHER = "Ministry of Food and Agriculture (SRID), via FAO's FPMA tool"
CACHE_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 30
MAX_EVIDENCE = 8

# WFP still uses the 10 regions from before 2019. Those not split since map directly.
ADMIN1_REGION = {
    "Ashanti": "Ashanti",
    "Central": "Central",
    "Eastern": "Eastern",
    "Greater Accra": "Greater Accra",
    "Upper East": "Upper East",
    "Upper West": "Upper West",
}
# Markets in a split region, placed in today's region. A market not listed is left out rather
# than guessed.
MARKET_REGION = {
    "techiman": "Bono East", "kintampo": "Bono East", "atebubu": "Bono East", "yeji": "Bono East",
    "nkoranza": "Bono East", "sunyani": "Bono", "wenchi": "Bono", "berekum": "Bono",
    "dormaa ahenkro": "Bono", "goaso": "Ahafo", "bechem": "Ahafo",
    "tamale": "Northern", "yendi": "Northern", "bimbilla": "Northern", "savelugu": "Northern",
    "damongo": "Savannah", "bole": "Savannah", "salaga": "Savannah", "buipe": "Savannah",
    "walewale": "North East", "nalerigu": "North East", "gambaga": "North East",
    "ho": "Volta", "hohoe": "Volta", "kpando": "Volta", "aflao": "Volta", "keta": "Volta",
    "dambai": "Oti", "nkwanta": "Oti", "kete krachi": "Oti", "jasikan": "Oti", "kadjebi": "Oti",
    "takoradi": "Western", "sekondi": "Western", "tarkwa": "Western", "axim": "Western",
    "sefwi wiawso": "Western North", "bibiani": "Western North", "enchi": "Western North",
    "juaboso": "Western North",
}
# FPMA's Ghana markets that MARKET_REGION doesn't already place.
FPMA_MARKET_REGION = {
    "accra (agbogbloshie)": "Greater Accra",
    "kumasi": "Ashanti",
    "bolgatanga": "Upper East",
    "wa": "Upper West",
}
# The app's crops, as WFP names them ("Maize (white)" counts as maize).
CROP_ALIASES = {
    "groundnut": "groundnuts",
    "plantain": "plantains",
    "tomato": "tomatoes",
    "pepper": "peppers",
    "cowpea": "cowpeas",
    "soybean": "soybeans",
    "oil palm": "oil (palm)",
    "onion": "onions",
    "mango": "mangoes",
}


@dataclass(frozen=True)
class Observation:
    day: date
    region: str
    market: str
    commodity: str
    unit: str
    quantity: float
    package_kg: float | None
    price: float
    price_type: str
    source: str = "wfp"  # or "fpma"


# Per source: the evidence's source block (title, publisher, link, kind).
SOURCES = {
    "wfp": {"label": "WFP food prices for Ghana", "publisher": PUBLISHER, "url": DATASET_PAGE, "kind": "market-report"},
    "fpma": {"label": "MoFA wholesale prices", "publisher": FPMA_PUBLISHER, "url": FPMA_PAGE, "kind": "official"},
}


class PricesUnavailable(Exception):
    pass


_cache: tuple[float, list[Observation]] | None = None
_lock = threading.Lock()


def _region_of(admin1: str, market: str) -> str | None:
    region = ADMIN1_REGION.get(admin1.strip())
    if region:
        return region
    region = MARKET_REGION.get(market.strip().lower())
    return region if region in REGIONS else None


def _unit(raw: str) -> tuple[str, float, float | None] | None:
    """WFP "KG" -> ("kg", 1, None); "50 KG" -> ("bag", 1, 50); "100 KG" -> ("bag", 1, 100);
    "MT" -> ("tonne", 1, None). Other units (bowls, pieces, litres) are not comparable."""
    text = raw.strip().upper()
    if text in ("KG", "1 KG"):
        return "kg", 1, None
    if text in ("MT", "TONNE", "1 MT"):
        return "tonne", 1, None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*KG", text)
    if match:
        return "bag", 1, float(match.group(1))
    return None


def parse_csv(text: str) -> list[Observation]:
    """The HDX file (header row, then a HXL tag row starting "#date"). Bad rows are skipped."""
    rows = csv.DictReader(io.StringIO(text))
    out: list[Observation] = []
    for row in rows:
        if (row.get("date") or "").startswith("#"):
            continue
        try:
            if (row.get("currency") or "").strip().upper() != "GHS":
                continue
            unit = _unit(row.get("unit") or "")
            region = _region_of(row.get("admin1") or "", row.get("market") or "")
            price = float(row["price"])
            day = date.fromisoformat((row.get("date") or "")[:10])
        except (KeyError, ValueError):
            continue
        if not unit or not region or price <= 0:
            continue
        out.append(
            Observation(
                day=day,
                region=region,
                market=(row.get("market") or "").strip(),
                commodity=(row.get("commodity") or "").strip(),
                unit=unit[0],
                quantity=unit[1],
                package_kg=unit[2],
                price=round(price, 2),
                price_type=(row.get("pricetype") or "").strip() or "Retail",
            )
        )
    return out


def _csv_url(client: httpx.Client) -> str:
    """The dataset's current CSV, found through the HDX catalogue (the file's URL changes)."""
    info = client.get(CATALOGUE).raise_for_status().json()
    resources = info.get("result", {}).get("resources", [])
    for resource in resources:
        url = resource.get("url") or ""
        # The prices file (wfp_food_prices_gha.csv), not the markets list or a QC file.
        file = url.rsplit("/", 1)[-1].lower()
        if (resource.get("format") or "").upper() == "CSV" and file.startswith("wfp_food_prices"):
            return url
    raise PricesUnavailable("The WFP dataset has no CSV file right now.")


def _download() -> str:
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(_csv_url(client))
        response.raise_for_status()
        return response.text


def parse_fpma(series: list[dict], prices: list[dict]) -> list[Observation]:
    """FPMA's Ghana series (market, commodity, unit) and their monthly datapoints. Each price is
    per the series' unit ("100 kg" is a 100 kg bag). Series in other currencies or units that
    aren't comparable (litres) are skipped, as are markets it can't place in a region."""
    by_id = {s.get("uuid"): s for s in series if s.get("iso3_country_code") == "GHA"}
    out: list[Observation] = []
    for item in prices:
        s = by_id.get(item.get("uuid"))
        if not s or (s.get("currency") or "").upper() != "GHS":
            continue
        unit = _unit(s.get("measure_unit_label") or "")
        market = (s.get("market_name") or "").strip()
        region = FPMA_MARKET_REGION.get(market.lower()) or MARKET_REGION.get(market.lower())
        if not unit or region not in REGIONS:
            continue
        for point in item.get("datapoints") or []:
            try:
                price = float(point["price_value"])
                day = date.fromisoformat(str(point["date"])[:10])
            except (KeyError, TypeError, ValueError):
                continue
            if point.get("periodicity", "monthly") != "monthly" or price <= 0:
                continue
            out.append(
                Observation(
                    day=day,
                    region=region,
                    market=market,
                    commodity=(s.get("commodity_name") or "").strip(),
                    unit=unit[0],
                    quantity=unit[1],
                    package_kg=unit[2],
                    price=round(price, 2),
                    price_type=(s.get("price_type") or "").strip().title() or "Wholesale",
                    source="fpma",
                )
            )
    return out


def _download_fpma() -> tuple[list[dict], list[dict]]:
    """FPMA's series list (every country: the API ignores a country filter), then the Ghana
    series' prices in one request."""
    with httpx.Client(timeout=TIMEOUT_SECONDS * 2, follow_redirects=True) as client:
        listing = client.get(f"{FPMA_API}/FpmaSerieDomestic/", params={"format": "json"}).raise_for_status().json()
        series = [s for s in listing.get("results", []) if s.get("iso3_country_code") == "GHA"]
        if not series:
            raise PricesUnavailable("FPMA lists no Ghana series right now.")
        ids = ",".join(s["uuid"] for s in series)
        url, params, prices = f"{FPMA_API}/FpmaSeriePrice/", {"format": "json", "uuid__in": ids}, []
        while url:
            page = client.get(url, params=params).raise_for_status().json()
            prices += page.get("results", [])
            url, params = page.get("next"), None
        return series, prices


def _load(source: str) -> list[Observation]:
    if source == "fpma":
        return parse_fpma(*_download_fpma())
    return parse_csv(_download())


def observations() -> list[Observation]:
    """Every usable observation from both sources (downloaded once a day). A source that fails
    is left out, and the result isn't cached, so the next research tries it again."""
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    rows: list[Observation] = []
    failures: list[str] = []
    for source in SOURCES:
        try:
            rows += _load(source)
        except (httpx.HTTPError, ValueError, KeyError, PricesUnavailable) as error:
            failures.append(f"{source}: {error}")
    if len(failures) == len(SOURCES):
        raise PricesUnavailable("; ".join(failures))
    if not failures:
        with _lock:
            _cache = (time.monotonic(), rows)
    return rows


def _crop_matches(crop: str, commodity: str) -> bool:
    wanted = CROP_ALIASES.get(crop.strip().lower(), crop.strip().lower())
    name = commodity.lower()
    return name == wanted or name.startswith(f"{wanted} (") or name.startswith(f"{wanted},")


def research(query: dict, today: date, rows: list[Observation]) -> dict | None:
    """The latest observation per market matching the query exactly, newest first, with the
    most recent one recommended. None when nothing matches."""
    region = query.get("region") or None
    market = (query.get("market") or "").strip().lower() or None
    matching = [
        o
        for o in rows
        if _crop_matches(query["crop"], o.commodity)
        and o.unit == query["unit"]
        and o.quantity == query["quantity"]
        and o.package_kg == query.get("packageWeightKg")
        and (not region or o.region == region)
        and (not market or o.market.lower() == market)
        and o.day <= today
    ]
    latest: dict[tuple[str, str, str], Observation] = {}
    for o in matching:
        key = (o.market, o.commodity, o.price_type)
        if key not in latest or o.day > latest[key].day:
            latest[key] = o
    chosen = sorted(latest.values(), key=lambda o: (o.day, o.market), reverse=True)[:MAX_EVIDENCE]
    if not chosen:
        return None
    evidence = [
        {
            "id": f"{o.source}-{o.day.isoformat()}-{re.sub(r'[^a-z0-9]+', '-', f'{o.market} {o.commodity} {o.price_type}'.lower()).strip('-')}",
            "country": "GH",
            "currency": "GHS",
            "crop": query["crop"],
            "region": o.region,
            "market": o.market,
            "unit": o.unit,
            "quantity": o.quantity,
            **({"packageWeightKg": o.package_kg} if o.package_kg else {}),
            "price": o.price,
            "observedAt": o.day.isoformat(),
            "source": {
                "title": f"{SOURCES[o.source]['label']}: {o.commodity}, {o.market} ({o.price_type.lower()})",
                "publisher": SOURCES[o.source]["publisher"],
                "url": SOURCES[o.source]["url"],
                "publishedAt": o.day.isoformat(),
                "kind": SOURCES[o.source]["kind"],
                "excerpt": f"{o.commodity}, {o.price_type}, {o.market}: GHS {o.price:,.2f} per {o.quantity:g} {o.unit}"
                + (f" ({o.package_kg:g} kg)" if o.package_kg else "")
                + f", {o.day:%B %Y}.",
            },
        }
        for o in chosen
    ]
    newest = chosen[0]
    age = (
        " That is over a year ago: neither source has published anything newer for this query, "
        "so treat it as background only."
        if (today - newest.day).days > 365
        else ""
    )
    return {
        "evidence": evidence,
        "recommendedEvidenceId": evidence[0]["id"],
        "explanation": (
            f"The most recent observation is {newest.market} in {newest.day:%B %Y}: GHS {newest.price:,.2f}."
            f"{age} Check the date and the market before publishing: prices move with the season."
        ),
    }
