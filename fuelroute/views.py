"""HTTP layer: thin request parsing around :mod:`fuelroute.services.planner`."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from fuelroute.models import FuelStation
from fuelroute.services.geocoding import GeocodingError
from fuelroute.services.planner import (
    GEOMETRY_MODES,
    PlanRequest,
    build_plan,
    plan_to_geojson,
)
from fuelroute.services.routing import RoutingError
from fuelroute.services.station_index import StationIndex

logger = logging.getLogger(__name__)


def _error(message: str, *, status: int, code: str, **extra) -> JsonResponse:
    body = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return JsonResponse(body, status=status, json_dumps_params={"indent": 2})


def _read_params(request: HttpRequest) -> dict:
    if request.method == "POST":
        content_type = (request.content_type or "").lower()
        if "application/json" in content_type:
            try:
                payload = json.loads(request.body or b"{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object.")
            return {k: v for k, v in payload.items()}
        return request.POST.dict()
    return request.GET.dict()


@csrf_exempt
@require_http_methods(["GET", "POST"])
def route_plan(request: HttpRequest) -> HttpResponse:
    """Plan a fuel-optimal route between two US locations.

    Query/body parameters:
        start           required - "City, ST" or "lat,lon"
        finish          required - "City, ST" or "lat,lon"
        corridor_miles  optional - how far off-route a station may be
        geometry        optional - simplified (default) | full | none
        format          optional - json (default) | geojson
    """
    try:
        params = _read_params(request)
    except ValueError as exc:
        return _error(str(exc), status=400, code="invalid_body")

    start = str(params.get("start") or "").strip()
    finish = str(params.get("finish") or "").strip()

    missing = [n for n, v in (("start", start), ("finish", finish)) if not v]
    if missing:
        return _error(
            f"Missing required parameter(s): {', '.join(missing)}.",
            status=400,
            code="missing_parameter",
            example="/api/v1/route-plan/?start=Dallas,%20TX&finish=Chicago,%20IL",
        )

    raw_corridor = params.get("corridor_miles")
    corridor = settings.DEFAULT_CORRIDOR_MILES
    if raw_corridor not in (None, ""):
        try:
            corridor = float(raw_corridor)
        except (TypeError, ValueError):
            return _error(
                "'corridor_miles' must be a number.", status=400, code="invalid_parameter"
            )
        if not 0 < corridor <= settings.MAX_CORRIDOR_MILES:
            return _error(
                f"'corridor_miles' must be between 0 and {settings.MAX_CORRIDOR_MILES}.",
                status=400,
                code="invalid_parameter",
            )

    geometry_mode = str(params.get("geometry") or "simplified").lower()
    if geometry_mode not in GEOMETRY_MODES:
        return _error(
            f"'geometry' must be one of: {', '.join(sorted(GEOMETRY_MODES))}.",
            status=400,
            code="invalid_parameter",
        )

    output_format = str(params.get("format") or "json").lower()
    if output_format not in {"json", "geojson"}:
        return _error(
            "'format' must be 'json' or 'geojson'.", status=400, code="invalid_parameter"
        )

    try:
        payload = build_plan(
            PlanRequest(
                start=start,
                finish=finish,
                corridor_miles=corridor,
                geometry="simplified" if output_format == "geojson" else geometry_mode,
            )
        )
    except GeocodingError as exc:
        return _error(exc.message, status=exc.status, code=exc.code)
    except RoutingError as exc:
        return _error(exc.message, status=exc.status, code=exc.code)
    except Exception:  # noqa: BLE001 - never leak a traceback to the client
        logger.exception("Unhandled error planning %r -> %r", start, finish)
        return _error(
            "An unexpected error occurred while planning the route.",
            status=500,
            code="internal_error",
        )

    if output_format == "geojson":
        return JsonResponse(plan_to_geojson(payload), json_dumps_params={"indent": 2})

    status = 200 if payload.get("fuel_plan") is not None else 422
    return JsonResponse(payload, status=status, json_dumps_params={"indent": 2})


@require_http_methods(["GET"])
def health(request: HttpRequest) -> JsonResponse:
    """Liveness probe plus a quick look at what data is loaded."""
    count = FuelStation.objects.count()
    return JsonResponse(
        {
            "status": "ok" if count else "no_data",
            "stations_loaded": count,
            "vehicle": {
                "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
                "miles_per_gallon": settings.VEHICLE_MPG,
            },
            "routing_provider": settings.OSRM_BASE_URL,
        }
    )


@require_http_methods(["GET"])
def stations(request: HttpRequest) -> JsonResponse:
    """Cheapest stations in the dataset, optionally filtered by state."""
    queryset = FuelStation.objects.all()
    state = (request.GET.get("state") or "").strip().upper()
    if state:
        queryset = queryset.filter(state=state)

    try:
        limit = min(max(int(request.GET.get("limit", 25)), 1), 200)
    except (TypeError, ValueError):
        limit = 25

    rows = queryset.order_by("retail_price")[:limit]
    return JsonResponse(
        {
            "count": queryset.count(),
            "returned": len(rows),
            "stations": [
                {
                    "opis_id": s.opis_id,
                    "name": s.name,
                    "address": s.address,
                    "city": s.city,
                    "state": s.state,
                    "retail_price": round(s.retail_price, 3),
                    "price_observations": s.price_observations,
                    "latitude": round(s.latitude, 6),
                    "longitude": round(s.longitude, 6),
                }
                for s in rows
            ],
        },
        json_dumps_params={"indent": 2},
    )


def index(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "fuelroute/index.html",
        {
            "station_count": FuelStation.objects.count(),
            "max_range": settings.VEHICLE_MAX_RANGE_MILES,
            "mpg": settings.VEHICLE_MPG,
            "corridor": settings.DEFAULT_CORRIDOR_MILES,
        },
    )


def map_view(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "fuelroute/map.html",
        {
            "start": request.GET.get("start", "Dallas, TX"),
            "finish": request.GET.get("finish", "Chicago, IL"),
            "corridor": request.GET.get("corridor_miles", ""),
        },
    )
