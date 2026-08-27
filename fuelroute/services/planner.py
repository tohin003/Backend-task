"""Request orchestration: geocode -> route -> locate stations -> optimise."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from fuelroute.services import geo
from fuelroute.services.geocoding import Location, resolve_location
from fuelroute.services.optimizer import FuelPlan, plan_refuelling
from fuelroute.services.routing import RouteResult, RoutingError, fetch_route
from fuelroute.services.station_index import StationIndex

logger = logging.getLogger(__name__)

GEOMETRY_MODES = {"simplified", "full", "none"}


@dataclass(slots=True)
class PlanRequest:
    start: str
    finish: str
    corridor_miles: float
    geometry: str = "simplified"


def _round_key(location: Location) -> str:
    # ~100 m granularity: two nearby inputs share a cached route.
    return f"{location.latitude:.3f},{location.longitude:.3f}"


def _cache_key(start: Location, finish: Location, corridor_miles: float) -> str:
    raw = json.dumps(
        {
            "s": _round_key(start),
            "f": _round_key(finish),
            "c": round(corridor_miles, 1),
            "r": settings.VEHICLE_MAX_RANGE_MILES,
            "m": settings.VEHICLE_MPG,
        },
        sort_keys=True,
    )
    return "fuelroute:plan:" + hashlib.sha1(raw.encode()).hexdigest()


def _route_cache_key(start: Location, finish: Location) -> str:
    raw = f"{_round_key(start)}|{_round_key(finish)}"
    return "fuelroute:osrm:" + hashlib.sha1(raw.encode()).hexdigest()


def _fetch_route_cached(start: Location, finish: Location) -> tuple[RouteResult, bool]:
    """Return the route plus whether it came from cache (no external call)."""
    key = _route_cache_key(start, finish)
    cached = cache.get(key)
    if cached is not None:
        import numpy as np

        return (
            RouteResult(
                coordinates=np.asarray(cached["coordinates"], dtype=np.float64),
                distance_miles=cached["distance_miles"],
                duration_seconds=cached["duration_seconds"],
            ),
            True,
        )

    result = fetch_route(
        (start.latitude, start.longitude), (finish.latitude, finish.longitude)
    )
    cache.set(
        key,
        {
            "coordinates": result.coordinates.tolist(),
            "distance_miles": result.distance_miles,
            "duration_seconds": result.duration_seconds,
        },
        settings.ROUTE_CACHE_SECONDS,
    )
    return result, False


def _serialise_stops(plan: FuelPlan, index: StationIndex) -> list[dict]:
    stops = []
    for position, stop in enumerate(plan.stops, start=1):
        station = index.record(stop.candidate.station_id)
        stops.append(
            {
                "sequence": position,
                "station": {
                    "opis_id": station.opis_id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "latitude": round(station.latitude, 6),
                    "longitude": round(station.longitude, 6),
                    "geocode_precision": station.geocode_precision,
                },
                "price_per_gallon": round(stop.candidate.price, 3),
                "distance_along_route_miles": round(
                    stop.candidate.distance_along_miles, 1
                ),
                "detour_from_route_miles": round(stop.candidate.offset_miles, 1),
                "gallons_purchased": round(stop.gallons, 2),
                "cost_usd": round(stop.cost, 2),
            }
        )
    return stops


def build_plan(request: PlanRequest) -> dict:
    """Produce the full API payload for a start/finish pair."""
    started = time.perf_counter()
    external_calls = 0

    start = resolve_location(request.start, field="start")
    finish = resolve_location(request.finish, field="finish")
    for location in (start, finish):
        if location.source == "nominatim":
            external_calls += 1

    key = _cache_key(start, finish, request.corridor_miles)
    cached_payload = cache.get(key)
    if cached_payload is not None:
        payload = json.loads(cached_payload)
        payload["meta"] = dict(payload["meta"])
        payload["meta"]["cached"] = True
        payload["meta"]["external_api_calls"] = 0
        # The stored timings describe the original computation, not this request;
        # reporting them next to "cached": true would be misleading.
        payload["meta"].pop("timings_ms", None)
        payload["meta"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return _apply_geometry_mode(payload, request.geometry)

    geocode_ms = (time.perf_counter() - started) * 1000

    mark = time.perf_counter()
    route, from_cache = _fetch_route_cached(start, finish)
    if not from_cache:
        external_calls += 1
    routing_ms = (time.perf_counter() - mark) * 1000

    mark = time.perf_counter()
    measured = geo.RouteGeometry.build(
        route.coordinates, total_miles=route.distance_miles
    )
    index = StationIndex.get()
    candidates = index.candidates_near_route(measured, request.corridor_miles)
    match_ms = (time.perf_counter() - mark) * 1000

    mark = time.perf_counter()
    plan = plan_refuelling(
        candidates,
        route.distance_miles,
        max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
        mpg=settings.VEHICLE_MPG,
    )
    optimise_ms = (time.perf_counter() - mark) * 1000

    simplified = geo.simplify_polyline(route.coordinates, tolerance_deg=0.002)

    payload: dict = {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "route": {
            "total_distance_miles": round(route.distance_miles, 1),
            "estimated_duration_hours": round(route.duration_seconds / 3600.0, 2),
            "bounds": {k: round(v, 6) for k, v in measured.bounds().items()},
            "provider": "OSRM (OpenStreetMap)",
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(x, 5), round(y, 5)] for x, y in simplified],
            },
        },
        "vehicle": {
            "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
            "miles_per_gallon": settings.VEHICLE_MPG,
            "tank_capacity_gallons": round(
                settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG, 1
            ),
        },
        "meta": {
            "external_api_calls": external_calls,
            "cached": False,
            "stations_considered": len(candidates),
            "route_vertices": int(len(route.coordinates)),
            "corridor_miles": request.corridor_miles,
            "warnings": list(plan.warnings),
            "timings_ms": {
                "geocoding": round(geocode_ms, 1),
                "routing_api": round(routing_ms, 1),
                "station_matching": round(match_ms, 1),
                "optimisation": round(optimise_ms, 1),
            },
        },
    }

    if not plan.feasible:
        payload["fuel_plan"] = None
        payload["error"] = {
            "code": plan.reason,
            "message": (plan.detail or {}).get("message", "No feasible fuel plan."),
            "detail": plan.detail or {},
            "hint": (
                "Try increasing 'corridor_miles' to consider stations further "
                "from the route."
            ),
        }
    else:
        origin_fill = None
        if plan.origin_fill:
            station = index.record(plan.origin_fill.station_id)
            origin_fill = {
                "description": (
                    "Fuel for the origin -> first stop leg, bought before departure "
                    "at the first stop's price."
                ),
                "leg_miles": round(plan.origin_fill.distance_miles, 1),
                "gallons": round(plan.origin_fill.gallons, 2),
                "price_per_gallon": round(plan.origin_fill.price, 3),
                "cost_usd": round(plan.origin_fill.cost, 2),
                "priced_at": f"{station.name}, {station.city}, {station.state}",
            }

        payload["fuel_plan"] = {
            "total_cost_usd": round(plan.total_cost, 2),
            "total_gallons": round(plan.total_gallons, 2),
            "average_price_per_gallon": round(plan.average_price, 3),
            "fuel_stop_count": len(plan.stops),
            "origin_fill": origin_fill,
            "stops": _serialise_stops(plan, index),
        }

    cache.set(key, json.dumps(payload), settings.ROUTE_CACHE_SECONDS)
    payload["meta"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return _apply_geometry_mode(payload, request.geometry)


def _apply_geometry_mode(payload: dict, mode: str) -> dict:
    if mode == "none":
        payload = dict(payload)
        payload["route"] = dict(payload["route"])
        payload["route"].pop("geometry", None)
    return payload


def plan_to_geojson(payload: dict) -> dict:
    """Render a plan payload as a GeoJSON FeatureCollection."""
    features = [
        {
            "type": "Feature",
            "geometry": payload["route"].get(
                "geometry", {"type": "LineString", "coordinates": []}
            ),
            "properties": {
                "kind": "route",
                "total_distance_miles": payload["route"]["total_distance_miles"],
                "estimated_duration_hours": payload["route"]["estimated_duration_hours"],
            },
        }
    ]

    fuel_plan = payload.get("fuel_plan") or {}
    for stop in fuel_plan.get("stops", []):
        station = stop["station"]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [station["longitude"], station["latitude"]],
                },
                "properties": {
                    "kind": "fuel_stop",
                    "sequence": stop["sequence"],
                    "name": station["name"],
                    "city": station["city"],
                    "state": station["state"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "gallons_purchased": stop["gallons_purchased"],
                    "cost_usd": stop["cost_usd"],
                    "distance_along_route_miles": stop["distance_along_route_miles"],
                },
            }
        )

    for role in ("start", "finish"):
        node = payload[role]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [node["longitude"], node["latitude"]],
                },
                "properties": {"kind": role, "name": node["resolved_to"]},
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "total_cost_usd": fuel_plan.get("total_cost_usd"),
            "total_gallons": fuel_plan.get("total_gallons"),
        },
    }
