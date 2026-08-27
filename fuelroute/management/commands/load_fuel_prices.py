"""Load the fuel-price CSV into the database, joined with offline coordinates."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelroute.models import FuelStation

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


class Command(BaseCommand):
    help = "Load truck-stop fuel prices from the assessment CSV into the database."

    def add_arguments(self, parser):
        parser.add_argument("--csv", type=Path, default=settings.FUEL_PRICES_CSV)
        parser.add_argument(
            "--coordinates", type=Path, default=settings.DATA_DIR / "city_coordinates.csv"
        )

    def handle(self, *args, **options):
        csv_path: Path = options["csv"]
        coords_path: Path = options["coordinates"]

        if not csv_path.exists():
            raise CommandError(f"Price CSV not found: {csv_path}")
        if not coords_path.exists():
            raise CommandError(
                f"Coordinate file not found: {coords_path}\n"
                "Run: python scripts/build_station_coordinates.py"
            )

        coordinates: dict[tuple[str, str], tuple[float, float, str]] = {}
        with coords_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                coordinates[(row["city_key"], row["state"])] = (
                    float(row["latitude"]),
                    float(row["longitude"]),
                    row["source"],
                )

        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        # The CSV holds several price observations per truck stop (same OPIS id,
        # same address, different retail price). Collapse them into one station
        # carrying the mean observed price.
        grouped: dict[str, list[dict]] = defaultdict(list)
        skipped_non_us = 0
        for row in rows:
            state = row["State"].strip().upper()
            if state not in US_STATES:
                skipped_non_us += 1
                continue
            grouped[row["OPIS Truckstop ID"].strip()].append(row)

        stations: list[FuelStation] = []
        missing_coords: list[str] = []
        bad_price = 0

        for opis_id, entries in grouped.items():
            prices = []
            for entry in entries:
                try:
                    value = float(entry["Retail Price"])
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    prices.append(value)
            if not prices:
                bad_price += 1
                continue

            # Prefer the most descriptive of the duplicate names.
            head = max(entries, key=lambda e: len(e["Truckstop Name"].strip()))
            city = head["City"].strip()
            state = head["State"].strip().upper()
            located = coordinates.get((normalise(city), state))
            if located is None:
                missing_coords.append(f"{city}, {state}")
                continue

            latitude, longitude, source = located
            stations.append(
                FuelStation(
                    opis_id=opis_id,
                    name=head["Truckstop Name"].strip(),
                    address=head["Address"].strip(),
                    city=city,
                    state=state,
                    rack_id=head["Rack ID"].strip(),
                    retail_price=round(sum(prices) / len(prices), 4),
                    price_observations=len(prices),
                    latitude=latitude,
                    longitude=longitude,
                    geocode_precision=source,
                )
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)

        self.stdout.write(f"CSV rows read              : {len(rows)}")
        self.stdout.write(f"Non-US rows skipped        : {skipped_non_us}")
        self.stdout.write(f"Unique truck stops         : {len(grouped)}")
        self.stdout.write(f"Skipped (no coordinates)   : {len(missing_coords)}")
        self.stdout.write(f"Skipped (no valid price)   : {bad_price}")
        self.stdout.write(
            self.style.SUCCESS(f"Loaded {len(stations)} fuel stations.")
        )
        if missing_coords:
            self.stdout.write(
                self.style.WARNING(f"  missing coordinates for: {sorted(set(missing_coords))}")
            )
