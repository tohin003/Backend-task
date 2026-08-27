#!/usr/bin/env python
"""One-time offline build step: resolve coordinates for the fuel-price CSV.

The assessment CSV identifies each truck stop only by street/exit description,
city and state - there are no coordinates, and routing against 6,600 stations
needs them. Geocoding at request time would be far too slow and would hammer a
third-party service, so we resolve everything **once**, offline, and commit the
result.

Two artefacts are produced:

  data/city_coordinates.csv - one row per (city, state) in the price file
  data/us_places.csv        - a compact US gazetteer used at request time to
                              resolve free-text start/finish inputs without a
                              network call

Source: GeoNames (https://download.geonames.org/export/dump/US.zip), CC BY 4.0.
Anything GeoNames cannot resolve falls back to Nominatim at 1 request/second.

Usage:
    python scripts/build_station_coordinates.py [--skip-nominatim]

The repo ships with both CSVs already generated; you only need to re-run this
if the price file changes.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / ".geonames_cache"
GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "fuel-route-api/1.0 (backend assessment; offline build step)"

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

DIRECTIONS = {
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
    "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST",
}

# Minimum population for a place to be carried in the runtime gazetteer. Places
# that appear in the price file are always included regardless of population.
MIN_PLACE_POPULATION = 200

# GeoNames stores New York City as "New York City", so a user typing
# "New York, NY" would miss. Carrying alternate names for larger places fixes
# that class of miss without bloating the file.
ALT_NAME_MIN_POPULATION = 5000


def normalise(name: str) -> str:
    """Loose but order-preserving normalisation: 'St. Louis' -> 'ST LOUIS'."""
    text = name.strip().upper().replace(".", "").replace(",", "")
    text = re.sub(r"[^A-Z0-9 '\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^SAINTE\b", "STE", text)
    text = re.sub(r"^SAINT\b", "ST", text)
    first, _, rest = text.partition(" ")
    if rest and first in DIRECTIONS:
        text = f"{DIRECTIONS[first]} {rest}"
    return text


def squash(name: str) -> str:
    """Aggressive fallback key: 'Mc Lean' and 'McLean' both -> 'MCLEAN'."""
    return re.sub(r"[^A-Z0-9]", "", normalise(name))


def download_geonames() -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    target = CACHE_DIR / "US.txt"
    if target.exists():
        print(f"  using cached {target}")
        return target
    print(f"  downloading {GEONAMES_URL} (~70 MB)...")
    req = urllib.request.Request(GEONAMES_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        target.write_bytes(zf.read("US.txt"))
    print(f"  extracted to {target}")
    return target


def load_gazetteer(path: Path) -> tuple[dict, dict, list]:
    """Build exact and squashed name -> coordinate lookups from GeoNames."""
    exact: dict[tuple[str, str], tuple] = {}
    squashed: dict[tuple[str, str], tuple] = {}
    places: list[tuple[str, str, float, float, int, list[str]]] = []
    rank = {"P": 0, "A": 1}

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 15:
                continue
            feature_class, country, state = cols[6], cols[8], cols[10]
            if country != "US" or state not in US_STATES or feature_class not in rank:
                continue
            try:
                lat, lon, population = float(cols[4]), float(cols[5]), int(cols[14] or 0)
            except ValueError:
                continue

            # Prefer real populated places, then the most populous match.
            score = (rank[feature_class], -population)
            if feature_class == "P":
                alternates: list[str] = []
                if population >= ALT_NAME_MIN_POPULATION and cols[3]:
                    for alt in cols[3].split(","):
                        alt = alt.strip()
                        if (
                            2 <= len(alt) <= 40
                            and alt.isascii()
                            and any(ch.isalpha() for ch in alt)
                        ):
                            alternates.append(alt)
                places.append((cols[1], state, lat, lon, population, alternates))

            names = {cols[1], cols[2]}
            if cols[3]:
                names.update(cols[3].split(","))
            for raw in names:
                if not raw.strip():
                    continue
                key = (normalise(raw), state)
                if key not in exact or score < exact[key][0]:
                    exact[key] = (score, lat, lon)
                skey = (squash(raw), state)
                if skey not in squashed or score < squashed[skey][0]:
                    squashed[skey] = (score, lat, lon)

    return exact, squashed, places


def nominatim_lookup(city: str, state: str) -> tuple[float, float] | None:
    """Look up a single city. Uses requests (bundles certifi) when available,
    because the stock macOS Python often has no usable CA bundle for urllib."""
    query = {"city": city, "state": state, "country": "USA", "format": "json", "limit": 1}
    try:
        try:
            import requests

            resp = requests.get(
                NOMINATIM_URL, params=query, headers={"User-Agent": USER_AGENT}, timeout=20
            )
            resp.raise_for_status()
            payload = resp.json()
        except ImportError:
            req = urllib.request.Request(
                f"{NOMINATIM_URL}?{urllib.parse.urlencode(query)}",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=20) as handle:
                payload = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - best effort fallback
        print(f"    nominatim error for {city}, {state}: {exc}")
        return None
    if not payload:
        return None
    return float(payload[0]["lat"]), float(payload[0]["lon"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-nominatim", action="store_true")
    parser.add_argument("--prices", type=Path, default=DATA_DIR / "fuel-prices-for-be-assessment.csv")
    args = parser.parse_args()

    if not args.prices.exists():
        print(f"price file not found: {args.prices}", file=sys.stderr)
        return 1

    print("1. Loading price file...")
    with args.prices.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    wanted: dict[tuple[str, str], str] = {}
    skipped_non_us = 0
    for row in rows:
        state = row["State"].strip().upper()
        city = row["City"].strip()
        if state not in US_STATES:
            skipped_non_us += 1
            continue
        wanted.setdefault((normalise(city), state), city)
    print(f"   {len(rows)} rows, {len(wanted)} unique US city/state pairs "
          f"({skipped_non_us} non-US rows ignored)")

    print("2. Loading GeoNames gazetteer...")
    exact, squashed, places = load_gazetteer(download_geonames())
    print(f"   {len(exact)} exact keys, {len(places)} populated places")

    # Hand-checked coordinates for the handful of places GeoNames does not carry
    # under the name the price file uses. Committed so this build is fully
    # reproducible with no network access.
    manual: dict[tuple[str, str], tuple[float, float]] = {}
    manual_path = DATA_DIR / "manual_coordinates.csv"
    if manual_path.exists():
        with manual_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                manual[(row["city_key"], row["state"])] = (
                    float(row["latitude"]),
                    float(row["longitude"]),
                )
        print(f"   loaded {len(manual)} manual coordinate overrides")

    print("3. Resolving station cities...")
    resolved: dict[tuple[str, str], tuple[float, float, str]] = {}
    unresolved: list[tuple[str, str]] = []
    for (norm_city, state), original in sorted(wanted.items()):
        override = manual.get((norm_city, state))
        if override is not None:
            resolved[(norm_city, state)] = (override[0], override[1], "manual")
            continue
        hit = exact.get((norm_city, state))
        source = "geonames"
        if hit is None:
            hit = squashed.get((squash(original), state))
            source = "geonames-fuzzy"
        if hit is None:
            unresolved.append((norm_city, state))
            continue
        resolved[(norm_city, state)] = (hit[1], hit[2], source)
    print(f"   resolved {len(resolved)}, unresolved {len(unresolved)}")

    if unresolved and not args.skip_nominatim:
        print(f"4. Nominatim fallback for {len(unresolved)} cities (1 req/sec)...")
        still_missing = []
        for norm_city, state in unresolved:
            original = wanted[(norm_city, state)]
            found = nominatim_lookup(original, state)
            if found:
                resolved[(norm_city, state)] = (found[0], found[1], "nominatim")
                print(f"    {original}, {state} -> {found[0]:.4f}, {found[1]:.4f}")
            else:
                still_missing.append((original, state))
            time.sleep(1.1)
        unresolved = still_missing
        if unresolved:
            print(f"   still unresolved: {unresolved}")

    DATA_DIR.mkdir(exist_ok=True)

    city_path = DATA_DIR / "city_coordinates.csv"
    with city_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["city_key", "state", "latitude", "longitude", "source"])
        for (norm_city, state), (lat, lon, source) in sorted(resolved.items()):
            writer.writerow([norm_city, state, f"{lat:.5f}", f"{lon:.5f}", source])
    print(f"5. Wrote {city_path} ({len(resolved)} rows)")

    # Runtime gazetteer: everything reasonably populated, plus every city that
    # appears in the price file so those are always resolvable.
    station_keys = set(resolved)
    # rank 0 = the place's own name, rank 1 = an alternate name. A primary name
    # always wins a key collision; ties break on population.
    best_place: dict[tuple[str, str], tuple[int, int, str, float, float]] = {}

    def offer(key_name: str, state: str, display: str, lat: float, lon: float,
              population: int, rank: int) -> None:
        key = (normalise(key_name), state)
        if not key[0]:
            return
        if rank == 0 and population < MIN_PLACE_POPULATION and key not in station_keys:
            return
        current = best_place.get(key)
        if current is None or (rank, -population) < (current[0], -current[1]):
            best_place[key] = (rank, population, display, lat, lon)

    for name, state, lat, lon, population, alternates in places:
        offer(name, state, name, lat, lon, population, 0)
    for name, state, lat, lon, population, alternates in places:
        for alt in alternates:
            offer(alt, state, name, lat, lon, population, 1)

    places_path = DATA_DIR / "us_places.csv"
    with places_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name_key", "display_name", "state", "latitude", "longitude", "population"])
        for (key, state), (_rank, population, name, lat, lon) in sorted(best_place.items()):
            writer.writerow([key, name, state, f"{lat:.5f}", f"{lon:.5f}", population])
    print(f"6. Wrote {places_path} ({len(best_place)} rows)")

    if unresolved:
        print(f"\nWARNING: {len(unresolved)} cities unresolved: {unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
