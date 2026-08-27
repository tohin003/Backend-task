"""Resolve free-text start/finish inputs to coordinates.

Ordered cheapest-first so the common case costs nothing:

1. ``"lat,lon"`` literals            -> parsed directly, zero network calls
2. the bundled US gazetteer          -> in-process lookup, zero network calls
3. Nominatim                         -> one HTTP call, only for inputs the
                                        gazetteer cannot resolve (full street
                                        addresses, ZIP codes, obscure places)

Because the gazetteer ships with the repo and covers ~27k US places, a typical
"City, ST" request reaches OSRM as the *only* external call.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC", "WASHINGTON DC": "DC",
}

DIRECTIONS = {
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
    "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST",
}

# The price file itself contains Canadian truck stops, and "Toronto, ON" would
# otherwise be fuzzy-matched to a US street of the same name. Reject these up
# front with a clear message instead.
CANADIAN_REGIONS = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
CANADIAN_REGION_NAMES = {v.upper(): k for k, v in CANADIAN_REGIONS.items()}

# Generous bounding box covering the 50 states; used to reject obviously
# non-US coordinate input.
US_BOUNDS = (18.0, -179.9, 72.0, -66.0)  # min_lat, min_lon, max_lat, max_lon

_LATLON_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$"
)


class GeocodingError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "geocoding_failed"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(slots=True)
class Location:
    query: str
    latitude: float
    longitude: float
    display_name: str
    source: str
    state: str | None = None

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "resolved_to": self.display_name,
            "latitude": round(self.latitude, 6),
            "longitude": round(self.longitude, 6),
            "source": self.source,
        }


def normalise(name: str) -> str:
    """Must stay in sync with scripts/build_station_coordinates.py."""
    text = name.strip().upper().replace(".", "").replace(",", "")
    text = re.sub(r"[^A-Z0-9 '\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^SAINTE\b", "STE", text)
    text = re.sub(r"^SAINT\b", "ST", text)
    first, _, rest = text.partition(" ")
    if rest and first in DIRECTIONS:
        text = f"{DIRECTIONS[first]} {rest}"
    return text


class Gazetteer:
    """In-memory index of ``data/us_places.csv``, loaded once per process."""

    def __init__(self) -> None:
        self._by_name_state: dict[tuple[str, str], tuple[float, float, str]] = {}
        self._by_name: dict[str, tuple[int, float, float, str, str]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            path = settings.US_PLACES_CSV
            if not path.exists():
                logger.warning("Gazetteer missing at %s; falling back to Nominatim", path)
                self._loaded = True
                return
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    key = row["name_key"]
                    state = row["state"]
                    lat = float(row["latitude"])
                    lon = float(row["longitude"])
                    population = int(row["population"] or 0)
                    display = f"{row['display_name']}, {state}"
                    self._by_name_state[(key, state)] = (lat, lon, display)
                    best = self._by_name.get(key)
                    if best is None or population > best[0]:
                        self._by_name[key] = (population, lat, lon, display, state)
            self._loaded = True
            logger.info("Loaded gazetteer: %d places", len(self._by_name_state))

    def lookup(self, city: str, state: str | None) -> tuple[float, float, str, str] | None:
        self.load()
        key = normalise(city)
        if state:
            hit = self._by_name_state.get((key, state))
            if hit:
                return hit[0], hit[1], hit[2], state
            return None
        hit = self._by_name.get(key)
        if hit:
            return hit[1], hit[2], hit[3], hit[4]
        return None


_GAZETTEER = Gazetteer()


def split_state(text: str) -> tuple[str, str | None]:
    """Pull a trailing state out of ``"Dallas, TX"`` / ``"Dallas Texas"``."""
    cleaned = re.sub(r",?\s*(USA|US|UNITED STATES)\s*$", "", text.strip(), flags=re.I)

    if "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        candidate = tail.strip().upper()
        if candidate in US_STATES:
            return head.strip(), candidate
        if candidate in STATE_NAMES:
            return head.strip(), STATE_NAMES[candidate]

    parts = cleaned.split()
    if len(parts) >= 2:
        if parts[-1].upper() in US_STATES:
            return " ".join(parts[:-1]).strip(), parts[-1].upper()
        for size in (3, 2, 1):
            if len(parts) > size:
                tail = " ".join(parts[-size:]).upper()
                if tail in STATE_NAMES:
                    return " ".join(parts[:-size]).strip(), STATE_NAMES[tail]

    return cleaned.strip(), None


def _in_us(lat: float, lon: float) -> bool:
    min_lat, min_lon, max_lat, max_lon = US_BOUNDS
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _nominatim(query: str) -> Location | None:
    if not settings.ENABLE_NOMINATIM_FALLBACK:
        return None

    from fuelroute.services.routing import get_session

    url = f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search"
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
        "addressdetails": 0,
    }
    try:
        response = get_session().get(
            url,
            params=params,
            timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - fallback is best-effort
        logger.warning("Nominatim lookup failed for %r: %s", query, exc)
        return None

    if not payload:
        return None
    hit = payload[0]
    return Location(
        query=query,
        latitude=float(hit["lat"]),
        longitude=float(hit["lon"]),
        display_name=hit.get("display_name", query),
        source="nominatim",
    )


def resolve_location(text: str, *, field: str = "location") -> Location:
    """Resolve one user-supplied location string to coordinates."""
    if not text or not text.strip():
        raise GeocodingError(f"'{field}' is required.", code="missing_parameter")

    raw = text.strip()

    # 0. Reject obviously non-US regions before spending a lookup on them.
    trailing = re.sub(r"[.\s]+$", "", raw).rpartition(",")[2].strip().upper()
    if not trailing:
        trailing = raw.split()[-1].upper() if raw.split() else ""
    region = None
    if trailing in CANADIAN_REGIONS:
        region = CANADIAN_REGIONS[trailing]
    elif trailing in CANADIAN_REGION_NAMES:
        region = trailing.title()
    if region:
        raise GeocodingError(
            f"'{field}' ({raw}) looks like a location in {region}, Canada. "
            "Both the start and finish must be within the USA.",
            code="outside_usa",
        )

    # 1. Explicit coordinates.
    match = _LATLON_RE.match(raw)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise GeocodingError(
                f"'{field}' has out-of-range coordinates: {raw}", code="invalid_coordinates"
            )
        if not _in_us(lat, lon):
            raise GeocodingError(
                f"'{field}' ({raw}) is outside the USA. Both locations must be in the USA.",
                code="outside_usa",
            )
        return Location(
            query=raw,
            latitude=lat,
            longitude=lon,
            display_name=f"{lat:.5f}, {lon:.5f}",
            source="coordinates",
        )

    # 2. Bundled gazetteer.
    city, state = split_state(raw)
    if city:
        hit = _GAZETTEER.lookup(city, state)
        if hit:
            lat, lon, display, resolved_state = hit
            return Location(
                query=raw,
                latitude=lat,
                longitude=lon,
                display_name=display,
                source="gazetteer",
                state=resolved_state,
            )

    # 3. Nominatim fallback.
    located = _nominatim(raw)
    if located is not None:
        if not _in_us(located.latitude, located.longitude):
            raise GeocodingError(
                f"'{field}' ({raw}) is outside the USA. Both locations must be in the USA.",
                code="outside_usa",
            )
        located.query = raw
        return located

    raise GeocodingError(
        f"Could not find '{raw}' in the USA. Try 'City, ST' (e.g. 'Dallas, TX') "
        "or a 'latitude,longitude' pair.",
        status=404,
        code="location_not_found",
    )
