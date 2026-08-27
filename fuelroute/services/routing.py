"""Client for the routing provider.

We use the public OSRM demo server (https://router.project-osrm.org): free, no
API key, no registration, OpenStreetMap data. Exactly **one** call is made per
uncached route request, and the response is cached so repeats cost nothing.

Set ``OSRM_BASE_URL`` to point at a self-hosted OSRM instance for production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fuelroute.services.polyline import decode as decode_polyline

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    """Raised when a route could not be obtained."""

    def __init__(self, message: str, *, status: int = 502, code: str = "routing_failed"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _build_session() -> requests.Session:
    """A pooled session with retries, so latency stays low and blips self-heal."""
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": settings.NOMINATIM_USER_AGENT})
    return session


_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


@dataclass(slots=True)
class RouteResult:
    coordinates: np.ndarray  # (N, 2) lon/lat
    distance_miles: float
    duration_seconds: float
    provider: str = "osrm"


def fetch_route(
    start: tuple[float, float], finish: tuple[float, float]
) -> RouteResult:
    """Fetch a driving route. Coordinates are ``(latitude, longitude)`` pairs.

    This is the single external routing call made per request.
    """
    coords = f"{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
    url = f"{settings.OSRM_BASE_URL.rstrip('/')}/route/v1/driving/{coords}"
    params = {
        # Full-resolution geometry is needed to place stations along the route
        # accurately; polyline6 carries exactly the same vertices as geojson but
        # is ~3x faster end to end on the public OSRM server.
        "overview": "full",
        "geometries": "polyline6",
        "alternatives": "false",
        "steps": "false",
        "annotations": "false",
    }

    try:
        response = get_session().get(
            url, params=params, timeout=settings.OSRM_TIMEOUT_SECONDS
        )
    except requests.Timeout as exc:
        raise RoutingError(
            "The routing service timed out. Please try again.",
            status=504,
            code="routing_timeout",
        ) from exc
    except requests.RequestException as exc:
        raise RoutingError(f"Could not reach the routing service: {exc}") from exc

    # OSRM signals "no route" with HTTP 400 and a JSON body, so read the body
    # before deciding what the status code means.
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if payload is None:
        raise RoutingError(
            f"Routing service returned HTTP {response.status_code} with a "
            "malformed body.",
            status=502,
        )

    code = payload.get("code")
    if code == "NoRoute":
        raise RoutingError(
            "No drivable route exists between those two locations. Both must be "
            "reachable by road within the contiguous USA.",
            status=422,
            code="no_route",
        )
    if code in {"NoSegment", "NoMatch"}:
        raise RoutingError(
            "One of the locations is not near a drivable road. Both the start "
            "and finish must be reachable by road within the USA.",
            status=422,
            code="no_road_nearby",
        )
    if code != "Ok":
        raise RoutingError(
            f"Routing service error: {payload.get('message') or code}", status=502
        )

    routes = payload.get("routes") or []
    if not routes:
        raise RoutingError("Routing service returned no routes.", status=502)

    route = routes[0]
    geometry = route.get("geometry")
    if isinstance(geometry, str):
        coordinates = decode_polyline(geometry, precision=6)
    else:  # a server configured to return GeoJSON
        coordinates = np.asarray(
            (geometry or {}).get("coordinates") or [], dtype=np.float64
        )

    if len(coordinates) < 2:
        raise RoutingError("Routing service returned an empty geometry.", status=502)

    return RouteResult(
        coordinates=coordinates,
        distance_miles=float(route["distance"]) / METERS_PER_MILE,
        duration_seconds=float(route.get("duration") or 0.0),
    )
