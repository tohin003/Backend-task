"""Tests for the minimum-cost refuelling dynamic program."""

from __future__ import annotations

import itertools
import random

from django.test import SimpleTestCase

from fuelroute.services.optimizer import (
    Candidate,
    collapse_to_buckets,
    plan_refuelling,
)

MPG = 10.0
RANGE = 500.0


def make(*specs: tuple[float, float]) -> list[Candidate]:
    return [
        Candidate(station_id=i, distance_along_miles=d, price=p, offset_miles=1.0)
        for i, (d, p) in enumerate(specs)
    ]


def brute_force(candidates, total, max_range, mpg, origin_window):
    """Exhaustively price every legal set of stops. Used as an oracle."""
    stations = sorted(candidates, key=lambda c: c.distance_along_miles)
    best = None
    window = min(origin_window, max_range)
    for size in range(1, len(stations) + 1):
        for combo in itertools.combinations(range(len(stations)), size):
            d = [stations[i].distance_along_miles for i in combo]
            if d[0] > window or total - d[-1] > max_range:
                continue
            if any(d[k + 1] - d[k] > max_range for k in range(len(d) - 1)):
                continue
            cost = stations[combo[0]].price * d[0] / mpg
            for k, i in enumerate(combo):
                end = total if k == len(combo) - 1 else d[k + 1]
                cost += stations[i].price * (end - d[k]) / mpg
            if best is None or cost < best:
                best = cost
    return best


class PlanRefuellingTests(SimpleTestCase):
    def test_single_station_prices_whole_trip(self):
        plan = plan_refuelling(
            make((10.0, 3.00)), 300.0, max_range_miles=RANGE, mpg=MPG
        )
        self.assertTrue(plan.feasible)
        self.assertAlmostEqual(plan.total_gallons, 30.0)
        self.assertAlmostEqual(plan.total_cost, 90.0)

    def test_total_gallons_always_matches_distance(self):
        """The whole trip is paid for: gallons == distance / mpg, always."""
        plan = plan_refuelling(
            make((5.0, 3.50), (400.0, 2.90), (800.0, 3.10)),
            1000.0,
            max_range_miles=RANGE,
            mpg=MPG,
        )
        self.assertTrue(plan.feasible)
        self.assertAlmostEqual(plan.total_gallons, 100.0, places=6)

    def test_buys_minimum_at_expensive_stop_to_reach_cheaper_one(self):
        """Classic behaviour: top up just enough to get to the cheap station."""
        plan = plan_refuelling(
            make((0.0, 5.00), (100.0, 2.00)),
            600.0,
            max_range_miles=RANGE,
            mpg=MPG,
        )
        self.assertTrue(plan.feasible)
        first, second = plan.stops
        self.assertAlmostEqual(first.gallons, 10.0)   # only enough for 100 miles
        self.assertAlmostEqual(second.gallons, 50.0)  # the rest, at the cheap price

    def test_prefers_cheaper_station_when_both_reachable(self):
        plan = plan_refuelling(
            make((10.0, 4.00), (20.0, 3.00)), 400.0, max_range_miles=RANGE, mpg=MPG
        )
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.stops[0].candidate.price, 3.00)

    def test_respects_range_limit_with_multiple_stops(self):
        stations = make(*[(float(i * 100), 3.00) for i in range(20)])
        plan = plan_refuelling(stations, 1900.0, max_range_miles=RANGE, mpg=MPG)
        self.assertTrue(plan.feasible)
        positions = [0.0] + [s.candidate.distance_along_miles for s in plan.stops]
        positions.append(1900.0)
        for a, b in zip(positions, positions[1:]):
            self.assertLessEqual(b - a, RANGE + 1e-9)

    def test_infeasible_when_gap_exceeds_range(self):
        plan = plan_refuelling(
            make((0.0, 3.00), (900.0, 3.00)), 1000.0, max_range_miles=RANGE, mpg=MPG
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "range_gap")
        self.assertGreater(plan.detail["gap_miles"], RANGE)

    def test_infeasible_with_no_stations(self):
        plan = plan_refuelling([], 300.0, max_range_miles=RANGE, mpg=MPG)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "no_stations")

    def test_zero_distance_is_free(self):
        plan = plan_refuelling(make((0.0, 3.00)), 0.0, max_range_miles=RANGE, mpg=MPG)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.total_cost, 0.0)
        self.assertEqual(plan.stops, [])

    def test_origin_window_prevents_prebuying_at_a_distant_bargain(self):
        """A far-away cheap station must not price the leg before it."""
        plan = plan_refuelling(
            make((5.0, 4.00), (300.0, 1.00)),
            600.0,
            max_range_miles=RANGE,
            mpg=MPG,
            origin_fill_max_miles=50.0,
        )
        self.assertAlmostEqual(plan.stops[0].candidate.price, 4.00)
        # 5 mi origin leg + 295 mi to the cheap stop, all at $4.00
        self.assertAlmostEqual(plan.total_cost, 4.00 * 30.0 + 1.00 * 30.0, places=6)

    def test_warns_when_no_station_near_the_origin(self):
        plan = plan_refuelling(
            make((200.0, 3.00)),
            400.0,
            max_range_miles=RANGE,
            mpg=MPG,
            origin_fill_max_miles=50.0,
        )
        self.assertTrue(plan.feasible)
        self.assertTrue(plan.warnings)

    def test_matches_brute_force_on_random_instances(self):
        rng = random.Random(20260827)
        for _ in range(150):
            count = rng.randint(1, 8)
            total = rng.uniform(60.0, 1500.0)
            max_range = rng.choice([250.0, 400.0, 500.0])
            window = rng.choice([50.0, 500.0])
            stations = [
                Candidate(i, round(rng.uniform(0, total), 3), round(rng.uniform(2.5, 6.5), 3), 1.0)
                for i in range(count)
            ]
            plan = plan_refuelling(
                stations,
                total,
                max_range_miles=max_range,
                mpg=MPG,
                origin_fill_max_miles=window,
                bucket_miles=0.0,
            )
            expected = brute_force(stations, total, max_range, MPG, window)
            if expected is None:
                self.assertTrue(not plan.feasible or plan.warnings)
                continue
            if not plan.feasible:
                self.assertTrue(plan.warnings)
                continue
            self.assertAlmostEqual(plan.total_cost, expected, places=6)
            self.assertAlmostEqual(plan.total_gallons, total / MPG, places=6)


class BucketingTests(SimpleTestCase):
    def test_keeps_cheapest_per_bucket(self):
        collapsed = collapse_to_buckets(
            make((0.2, 3.50), (0.6, 3.10), (4.0, 3.90)), bucket_miles=1.0
        )
        self.assertEqual(len(collapsed), 2)
        self.assertAlmostEqual(collapsed[0].price, 3.10)

    def test_zero_bucket_disables_collapsing(self):
        stations = make((0.2, 3.50), (0.6, 3.10))
        self.assertEqual(len(collapse_to_buckets(stations, 0.0)), 2)

    def test_output_is_sorted_by_distance(self):
        collapsed = collapse_to_buckets(make((90.0, 3.0), (10.0, 3.0), (50.0, 3.0)), 1.0)
        distances = [c.distance_along_miles for c in collapsed]
        self.assertEqual(distances, sorted(distances))
