"""Naming a GPS position (reverse geocoding): village, town or city, district and Ghana's region.

Pure: it reads an OpenStreetMap Nominatim `address` block. Nominatim names Ghana's regions as
"Ashanti Region", "Greater Accra Region"..., a city or town under `city`/`town`, a village
under `village`/`hamlet`, a neighbourhood of a town under `suburb`, and the district under
`county`. In rural Ghana a village usually has no town above it, only its district (e.g. Krapa,
Ejisu Municipal District, Ashanti Region). Nothing is guessed: no town is made up from a
district's name, a region that is not one of Ghana's 16 is left out, and a position outside
Ghana says so."""

from app.domain.constants import REGIONS

_TOWN_KEYS = ("city", "town")
_VILLAGE_KEYS = ("village", "hamlet", "isolated_dwelling", "locality")
_AREA_KEYS = ("suburb", "neighbourhood", "quarter", "city_district")
_DISTRICT_KEYS = ("county", "district", "municipality", "state_district")
_BY_LOWER = {r.lower(): r for r in REGIONS}


def region_name(state: str | None) -> str | None:
    """'Ashanti Region' -> 'Ashanti'; anything that is not one of the 16 regions -> None."""
    if not state:
        return None
    name = state.strip()
    if name.lower().endswith(" region"):
        name = name[: -len(" region")]
    return _BY_LOWER.get(name.strip().lower())


def _first(address: dict, keys: tuple[str, ...]) -> str | None:
    return next((str(address[k]).strip() for k in keys if address.get(k) and str(address[k]).strip()), None)


def describe(address: dict | None) -> dict:
    """{village, area, town, district, region, inGhana} from a Nominatim address.

    - village: the village or hamlet (rural places);
    - area: the neighbourhood inside a town or city (e.g. Adum in Kumasi);
    - town: the town or city, only when OpenStreetMap names one;
    - district: e.g. "Ejisu Municipal District"; region: one of Ghana's 16, e.g. "Ashanti".
    Any of them may be None."""
    address = address or {}
    in_ghana = str(address.get("country_code", "")).lower() == "gh"
    town = _first(address, _TOWN_KEYS)
    village = _first(address, _VILLAGE_KEYS)
    area = _first(address, _AREA_KEYS)
    if area in (town, village):
        area = None
    if village == town:
        village = None
    return {
        "village": village,
        "area": area,
        "town": town,
        "district": _first(address, _DISTRICT_KEYS),
        "region": region_name(address.get("state") or address.get("region")) if in_ghana else None,
        "inGhana": in_ghana,
    }


def label(place: dict) -> str:
    """'Krapa, Ashanti, Ghana' / 'Kumasi, Ashanti, Ghana': the most specific named settlement
    (village, then town, then the district), the region and Ghana."""
    name = place.get("village") or place.get("town") or place.get("area") or place.get("district")
    if not place.get("inGhana"):
        return f"{name} (outside Ghana)" if name else "Your current location (outside Ghana)"
    return ", ".join(p for p in (name, place.get("region"), "Ghana") if p)
