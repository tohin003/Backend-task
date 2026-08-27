"""End-to-end tests for the HTTP API.

The routing provider is mocked throughout, so the suite is fast, deterministic
and needs no network access.
"""

from __future__ import annotations

import json
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fuelroute.models import FuelStation
from fuelroute.services.routing import RouteResult, RoutingError
from fuelroute.services.station_index import StationIndex

PLAN_URL = "/api/v1/route-plan/"


def straight_route(start_lon: float, end_lon: float, lat: float = 40.0, n: int = 800):
    lons = np.linspace(start_lon, end_lon, n)
    return np.column_stack((lons, np.full_like(lons, lat)))


class ApiTestCase(TestCase):
    """Shared fixtures: a straight east-west route with stations along it."""

    @classmethod
    def setUpTestData(cls):
        # Roughly 1,315 miles at latitude 40.
        cls.coords = straight_route(-100.0, -75.0)
        cls.distance = 1315.0
        specs = [
            ("A", -99.9, 3.60), ("B", -95.0, 3.20), ("C", -90.0, 2.80),
            ("D", -85.0, 3.40), ("E", -80.0, 3.00), ("F", -75.2, 3.90),
        ]
        FuelStation.objects.bulk_create(
            [
                FuelStation(
                    opis_id=f"T{i}", name=f"TEST STOP {name}", address="I-00, EXIT 1",
                    city=f"City{name}", state="KS", rack_id="1",
                    retail_price=price, price_observations=1,
                    latitude=40.02, longitude=lon, geocode_precision="test",
                )
                for i, (name, lon, price) in enumerate(specs)
            ]
        )

    def setUp(self):
        cache.clear()
        StationIndex.invalidate()
        self.addCleanup(StationIndex.invalidate)
        self.addCleanup(cache.clear)

    def patched_route(self, coords=None, distance=None, duration=68400.0):
        return mock.patch(
            "fuelroute.services.planner.fetch_route",
            return_value=RouteResult(
                coordinates=self.coords if coords is None else coords,
                distance_miles=self.distance if distance is None else distance,
                duration_seconds=duration,
            ),
        )


@override_settings(ENABLE_NOMINATIM_FALLBACK=False)
class RoutePlanTests(ApiTestCase):
    def test_happy_path_returns_a_complete_plan(self):
        with self.patched_route() as fetch:
            response = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_count, 1, "should make exactly one routing call")

        body = response.json()
        plan = body["fuel_plan"]
        self.assertGreater(plan["fuel_stop_count"], 0)
        self.assertAlmostEqual(plan["total_gallons"], self.distance / 10.0, places=1)
        self.assertGreater(plan["total_cost_usd"], 0)
        self.assertEqual(body["meta"]["external_api_calls"], 1)
        self.assertEqual(body["vehicle"]["max_range_miles"], 500.0)
        self.assertEqual(body["vehicle"]["miles_per_gallon"], 10.0)

    def test_stops_are_ordered_and_within_range(self):
        with self.patched_route():
            body = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            ).json()
        marks = [s["distance_along_route_miles"] for s in body["fuel_plan"]["stops"]]
        self.assertEqual(marks, sorted(marks))
        for a, b in zip([0.0] + marks, marks + [self.distance]):
            self.assertLessEqual(b - a, 500.0 + 1.0)

    def test_cost_equals_sum_of_stops_plus_origin_fill(self):
        with self.patched_route():
            plan = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            ).json()["fuel_plan"]
        total = sum(s["cost_usd"] for s in plan["stops"])
        if plan["origin_fill"]:
            total += plan["origin_fill"]["cost_usd"]
        self.assertAlmostEqual(total, plan["total_cost_usd"], places=1)

    def test_prefers_the_cheapest_reachable_station(self):
        with self.patched_route():
            plan = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            ).json()["fuel_plan"]
        prices = [s["price_per_gallon"] for s in plan["stops"]]
        self.assertIn(2.80, prices, "the cheapest station on the route should be used")

    def test_second_identical_request_is_served_from_cache(self):
        params = {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
        with self.patched_route() as fetch:
            first = self.client.get(PLAN_URL, params).json()
            second = self.client.get(PLAN_URL, params).json()
        self.assertEqual(fetch.call_count, 1, "cached request must not re-route")
        self.assertFalse(first["meta"]["cached"])
        self.assertTrue(second["meta"]["cached"])
        self.assertEqual(second["meta"]["external_api_calls"], 0)
        self.assertEqual(
            first["fuel_plan"]["total_cost_usd"], second["fuel_plan"]["total_cost_usd"]
        )

    def test_cached_response_does_not_report_stale_timings(self):
        """Timings describe the original computation, so a cache hit must drop them."""
        params = {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
        with self.patched_route():
            first = self.client.get(PLAN_URL, params).json()
            second = self.client.get(PLAN_URL, params).json()
        self.assertIn("timings_ms", first["meta"])
        self.assertNotIn("timings_ms", second["meta"])
        self.assertTrue(second["meta"]["cached"])

    def test_post_with_json_body_works(self):
        with self.patched_route():
            response = self.client.post(
                PLAN_URL,
                data=json.dumps({"start": "39.9,-100.0", "finish": "40.1,-75.0"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["fuel_plan"])

    def test_geojson_format(self):
        with self.patched_route():
            body = self.client.get(
                PLAN_URL,
                {"start": "39.9,-100.0", "finish": "40.1,-75.0", "format": "geojson"},
            ).json()
        self.assertEqual(body["type"], "FeatureCollection")
        kinds = {f["properties"]["kind"] for f in body["features"]}
        self.assertEqual({"route", "fuel_stop", "start", "finish"}, kinds)

    def test_geometry_none_omits_the_polyline(self):
        with self.patched_route():
            body = self.client.get(
                PLAN_URL,
                {"start": "39.9,-100.0", "finish": "40.1,-75.0", "geometry": "none"},
            ).json()
        self.assertNotIn("geometry", body["route"])

    def test_geometry_is_returned_by_default(self):
        with self.patched_route():
            body = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            ).json()
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertGreaterEqual(len(body["route"]["geometry"]["coordinates"]), 2)

    def test_zero_distance_route(self):
        with self.patched_route(coords=straight_route(-90.0, -90.0, n=2), distance=0.0):
            body = self.client.get(
                PLAN_URL, {"start": "40.0,-90.0", "finish": "40.0,-90.0"}
            ).json()
        self.assertEqual(body["fuel_plan"]["total_cost_usd"], 0.0)


@override_settings(ENABLE_NOMINATIM_FALLBACK=False)
class ValidationTests(ApiTestCase):
    def test_missing_finish(self):
        response = self.client.get(PLAN_URL, {"start": "Dallas, TX"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "missing_parameter")

    def test_missing_both(self):
        response = self.client.get(PLAN_URL)
        self.assertEqual(response.status_code, 400)

    def test_non_numeric_corridor(self):
        response = self.client.get(
            PLAN_URL, {"start": "40,-90", "finish": "40,-80", "corridor_miles": "wide"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_parameter")

    def test_corridor_above_maximum(self):
        response = self.client.get(
            PLAN_URL, {"start": "40,-90", "finish": "40,-80", "corridor_miles": "5000"}
        )
        self.assertEqual(response.status_code, 400)

    def test_bad_geometry_mode(self):
        response = self.client.get(
            PLAN_URL, {"start": "40,-90", "finish": "40,-80", "geometry": "wireframe"}
        )
        self.assertEqual(response.status_code, 400)

    def test_bad_format(self):
        response = self.client.get(
            PLAN_URL, {"start": "40,-90", "finish": "40,-80", "format": "xml"}
        )
        self.assertEqual(response.status_code, 400)

    def test_location_outside_usa(self):
        response = self.client.get(
            PLAN_URL, {"start": "48.8566,2.3522", "finish": "40,-80"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "outside_usa")

    def test_malformed_json_body(self):
        response = self.client.post(
            PLAN_URL, data="{not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_unsupported_method(self):
        self.assertEqual(self.client.delete(PLAN_URL).status_code, 405)


@override_settings(ENABLE_NOMINATIM_FALLBACK=False)
class FailureModeTests(ApiTestCase):
    def test_unreachable_route_is_reported_as_422(self):
        with mock.patch(
            "fuelroute.services.planner.fetch_route",
            side_effect=RoutingError("no route", status=422, code="no_route"),
        ):
            response = self.client.get(
                PLAN_URL, {"start": "40,-90", "finish": "40,-80"}
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "no_route")

    def test_routing_outage_is_reported_as_502(self):
        with mock.patch(
            "fuelroute.services.planner.fetch_route",
            side_effect=RoutingError("upstream down"),
        ):
            response = self.client.get(
                PLAN_URL, {"start": "40,-90", "finish": "40,-80"}
            )
        self.assertEqual(response.status_code, 502)

    def test_route_with_no_nearby_stations_returns_422(self):
        FuelStation.objects.all().delete()
        StationIndex.invalidate()
        with self.patched_route():
            response = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertIsNone(body["fuel_plan"])
        self.assertEqual(body["error"]["code"], "no_stations")
        self.assertIn("hint", body["error"])

    def test_gap_longer_than_range_returns_422(self):
        FuelStation.objects.exclude(opis_id__in=["T0"]).delete()
        StationIndex.invalidate()
        with self.patched_route():
            response = self.client.get(
                PLAN_URL, {"start": "39.9,-100.0", "finish": "40.1,-75.0"}
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "range_gap")

    def test_unexpected_error_is_not_leaked(self):
        with mock.patch(
            "fuelroute.services.planner.fetch_route", side_effect=ValueError("boom")
        ):
            response = self.client.get(
                PLAN_URL, {"start": "40,-90", "finish": "40,-80"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("boom", response.content.decode())


class SupportingEndpointTests(ApiTestCase):
    def test_health(self):
        body = self.client.get("/api/v1/health/").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["stations_loaded"], 6)

    def test_stations_sorted_by_price(self):
        body = self.client.get("/api/v1/stations/", {"limit": 3}).json()
        prices = [s["retail_price"] for s in body["stations"]]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(body["returned"], 3)

    def test_stations_filtered_by_state(self):
        body = self.client.get("/api/v1/stations/", {"state": "zz"}).json()
        self.assertEqual(body["count"], 0)

    def test_index_page_renders(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_map_page_renders(self):
        response = self.client.get("/map/", {"start": "Dallas, TX"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dallas, TX")
