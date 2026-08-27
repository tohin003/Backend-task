"""Tests for geometry helpers and the polyline decoder."""

from __future__ import annotations

import numpy as np
from django.test import SimpleTestCase

from fuelroute.services.geo import (
    RouteGeometry,
    chord_to_miles,
    cumulative_miles,
    haversine_miles,
    miles_to_chord,
    simplify_polyline,
)
from fuelroute.services.polyline import decode


class DistanceTests(SimpleTestCase):
    def test_haversine_known_pair(self):
        # New York -> Chicago is roughly 711 miles as the crow flies.
        miles = haversine_miles(40.7128, -74.0060, 41.8781, -87.6298)
        self.assertAlmostEqual(miles, 711.0, delta=3.0)

    def test_haversine_is_zero_for_identical_points(self):
        self.assertAlmostEqual(haversine_miles(35.0, -95.0, 35.0, -95.0), 0.0)

    def test_haversine_vectorises(self):
        out = haversine_miles(
            np.array([40.0, 41.0]), np.array([-74.0, -75.0]),
            np.array([41.0, 42.0]), np.array([-75.0, -76.0]),
        )
        self.assertEqual(out.shape, (2,))

    def test_chord_conversion_round_trips(self):
        for miles in (0.5, 15.0, 250.0):
            self.assertAlmostEqual(chord_to_miles(miles_to_chord(miles)), miles, places=6)

    def test_cumulative_starts_at_zero_and_increases(self):
        coords = np.array([[-74.0, 40.7], [-80.0, 41.0], [-87.6, 41.9]])
        cumulative = cumulative_miles(coords)
        self.assertEqual(cumulative[0], 0.0)
        self.assertTrue(np.all(np.diff(cumulative) > 0))
        self.assertAlmostEqual(cumulative[-1], 712.5, delta=2.0)

    def test_cumulative_handles_short_inputs(self):
        self.assertEqual(len(cumulative_miles(np.zeros((0, 2)))), 0)
        self.assertEqual(list(cumulative_miles(np.array([[-74.0, 40.7]]))), [0.0])


class SimplifyTests(SimpleTestCase):
    def test_collinear_points_are_dropped(self):
        coords = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]])
        out = simplify_polyline(coords, tolerance_deg=0.001)
        self.assertEqual(len(out), 2)

    def test_endpoints_are_always_kept(self):
        rng = np.random.default_rng(1)
        coords = np.column_stack((np.linspace(0, 10, 400), rng.normal(0, 0.5, 400)))
        out = simplify_polyline(coords, tolerance_deg=0.05)
        self.assertLess(len(out), len(coords))
        np.testing.assert_allclose(out[0], coords[0])
        np.testing.assert_allclose(out[-1], coords[-1])

    def test_short_input_is_returned_unchanged(self):
        coords = np.array([[0.0, 0.0], [1.0, 1.0]])
        np.testing.assert_allclose(simplify_polyline(coords, 0.1), coords)


class PolylineTests(SimpleTestCase):
    def test_decodes_reference_vector(self):
        """The canonical example from Google's polyline algorithm docs."""
        out = decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
        expected = np.array([[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]])
        np.testing.assert_allclose(out, expected, atol=1e-9)

    def test_empty_input(self):
        self.assertEqual(decode("").shape, (0, 2))

    def test_precision_six_round_trip(self):
        # Encode a known track at precision 6, then check we get it back.
        points = np.array([[-96.8, 32.78], [-95.5, 33.1], [-94.2, 34.9]])

        def encode(values: list[int]) -> str:
            out = []
            for value in values:
                v = ~(value << 1) if value < 0 else (value << 1)
                while v >= 0x20:
                    out.append(chr((0x20 | (v & 0x1F)) + 63))
                    v >>= 5
                out.append(chr(v + 63))
            return "".join(out)

        scaled = np.round(points * 1e6).astype(np.int64)
        deltas = np.diff(np.vstack(([[0, 0]], scaled)), axis=0)
        flat = [int(v) for lon, lat in deltas for v in (lat, lon)]
        np.testing.assert_allclose(decode(encode(flat), 6), points, atol=1e-9)


class RouteGeometryTests(SimpleTestCase):
    def setUp(self):
        # A straight east-west line at 40N, roughly 530 miles long.
        lons = np.linspace(-90.0, -80.0, 600)
        self.coords = np.column_stack((lons, np.full_like(lons, 40.0)))

    def test_total_miles_and_bounds(self):
        route = RouteGeometry.build(self.coords)
        self.assertAlmostEqual(route.total_miles, 527.0, delta=5.0)
        bounds = route.bounds()
        self.assertAlmostEqual(bounds["min_lon"], -90.0)
        self.assertAlmostEqual(bounds["max_lon"], -80.0)

    def test_total_miles_can_be_rescaled_to_the_router_value(self):
        route = RouteGeometry.build(self.coords, total_miles=600.0)
        self.assertAlmostEqual(route.total_miles, 600.0, places=6)

    def test_locate_finds_offset_and_distance_along(self):
        route = RouteGeometry.build(self.coords)
        # A point just north of the midpoint of the line.
        offset, along = route.locate(np.array([40.2]), np.array([-85.0]))
        self.assertAlmostEqual(float(offset[0]), 13.8, delta=1.5)
        self.assertAlmostEqual(float(along[0]), route.total_miles / 2, delta=5.0)

    def test_locate_handles_empty_input(self):
        route = RouteGeometry.build(self.coords)
        offset, along = route.locate(np.zeros(0), np.zeros(0))
        self.assertEqual(len(offset), 0)
        self.assertEqual(len(along), 0)
