"""In-memory spatial view over the fuel stations.

The station table is small (~6.6k rows) and effectively static, so we load it
into NumPy arrays once per process and reuse them for every request. Finding the
stations that sit along a route is then a single batched nearest-neighbour query
against the route's own KD-tree - no database round-trip, no per-station Python
loop.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from fuelroute.models import FuelStation
from fuelroute.services.geo import RouteGeometry
from fuelroute.services.optimizer import Candidate

logger = logging.getLogger(__name__)


class StationIndex:
    """Columnar snapshot of every geocoded fuel station."""

    _instance: "StationIndex | None" = None
    _lock = threading.Lock()

    def __init__(self, stations: list[FuelStation]) -> None:
        self.count = len(stations)
        self.ids = np.array([s.id for s in stations], dtype=np.int64)
        self.latitude = np.array([s.latitude for s in stations], dtype=np.float64)
        self.longitude = np.array([s.longitude for s in stations], dtype=np.float64)
        self.price = np.array([s.retail_price for s in stations], dtype=np.float64)
        self.records = {s.id: s for s in stations}

    @classmethod
    def get(cls) -> "StationIndex":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    stations = list(
                        FuelStation.objects.all().only(
                            "id", "opis_id", "name", "address", "city", "state",
                            "retail_price", "price_observations", "latitude",
                            "longitude", "geocode_precision",
                        )
                    )
                    cls._instance = cls(stations)
                    logger.info("StationIndex loaded: %d stations", len(stations))
        return cls._instance

    @classmethod
    def invalidate(cls) -> None:
        with cls._lock:
            cls._instance = None

    def candidates_near_route(
        self, route: RouteGeometry, corridor_miles: float
    ) -> list[Candidate]:
        """Stations within ``corridor_miles`` of the route, located along it."""
        if self.count == 0:
            return []

        # Cheap bounding-box prefilter before the KD-tree query.
        bounds = route.bounds()
        lat_pad = corridor_miles / 69.0
        mid_lat = np.radians((bounds["min_lat"] + bounds["max_lat"]) / 2.0)
        lon_pad = corridor_miles / max(69.0 * np.cos(mid_lat), 1e-6)

        mask = (
            (self.latitude >= bounds["min_lat"] - lat_pad)
            & (self.latitude <= bounds["max_lat"] + lat_pad)
            & (self.longitude >= bounds["min_lon"] - lon_pad)
            & (self.longitude <= bounds["max_lon"] + lon_pad)
        )
        selected = np.flatnonzero(mask)
        if selected.size == 0:
            return []

        offset, along = route.locate(
            self.latitude[selected], self.longitude[selected]
        )
        within = offset <= corridor_miles
        if not within.any():
            return []

        chosen = selected[within]
        return [
            Candidate(
                station_id=int(self.ids[idx]),
                distance_along_miles=float(dist),
                price=float(self.price[idx]),
                offset_miles=float(off),
            )
            for idx, dist, off in zip(chosen, along[within], offset[within])
        ]

    def record(self, station_id: int) -> FuelStation:
        return self.records[station_id]
