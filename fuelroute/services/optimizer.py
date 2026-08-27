"""Minimum-cost refuelling plan for a fixed route.

Problem
-------
Given stations at known distances along a route, a tank that covers at most
``max_range_miles``, and a fixed fuel economy, choose where to stop and how much
to buy so that the total spend is minimised.

Cost model (see README for the rationale)
-----------------------------------------
The driver pays for **every gallon burned on the trip**, so total gallons always
equals ``total_distance / mpg``. Fuel bought at a stop covers the leg from that
stop to the next one; the final stop covers the run to the destination. The
short leg from the origin to the *first* stop is paid for at that first stop's
price - i.e. the driver tops up before leaving at the price they are about to
pay anyway. That initial leg is capped (``origin_fill_max_miles``) so the plan
can never "pre-buy" a whole tank at a bargain price hundreds of miles away.

Algorithm
---------
Exact dynamic program over stations sorted by distance along the route:

    dp[j] = cheapest way to have paid for all fuel consumed over [0, d[j]],
            arriving at station j with an empty tank

    dp[j] = p[j] * d[j] / mpg                          (j is the first stop)
    dp[j] = min over i < j, d[j] - d[i] <= range of
                dp[i] + p[i] * (d[j] - d[i]) / mpg     (drove in from stop i)

    answer = min over j with D - d[j] <= range of
                dp[j] + p[j] * (D - d[j]) / mpg

The transition rearranges to ``(dp[i] - p[i]*d[i]/mpg) + (p[i]/mpg) * d[j]``,
which is a minimum over a sliding window of straight lines. Because the window
is bounded by the vehicle range, each step is a single vectorised NumPy
reduction over a short slice, so the whole DP runs in a few milliseconds even
for a coast-to-coast route.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

INFEASIBLE_GAP = "range_gap"
NO_STATIONS = "no_stations"


@dataclass(slots=True)
class Candidate:
    """A station considered as a possible stop, located along the route."""

    station_id: int
    distance_along_miles: float
    price: float
    offset_miles: float


@dataclass(slots=True)
class FuelStop:
    candidate: Candidate
    gallons: float
    cost: float


@dataclass(slots=True)
class OriginFill:
    """Fuel for the origin -> first stop leg, priced at the first stop."""

    gallons: float
    price: float
    cost: float
    station_id: int
    distance_miles: float


@dataclass(slots=True)
class FuelPlan:
    feasible: bool
    stops: list[FuelStop] = field(default_factory=list)
    origin_fill: OriginFill | None = None
    total_gallons: float = 0.0
    total_cost: float = 0.0
    reason: str | None = None
    detail: dict | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def average_price(self) -> float:
        return self.total_cost / self.total_gallons if self.total_gallons else 0.0


def collapse_to_buckets(
    candidates: list[Candidate], bucket_miles: float
) -> list[Candidate]:
    """Keep only the cheapest station within each short stretch of the route.

    Two truck stops a mile apart at the same interchange are interchangeable, so
    keeping the cheaper one bounds the size of the DP without changing the
    answer in any way that matters.
    """
    if bucket_miles <= 0 or not candidates:
        return sorted(candidates, key=lambda c: c.distance_along_miles)

    best: dict[int, Candidate] = {}
    for cand in candidates:
        key = int(cand.distance_along_miles // bucket_miles)
        current = best.get(key)
        if current is None or cand.price < current.price:
            best[key] = cand
    return sorted(best.values(), key=lambda c: c.distance_along_miles)


def plan_refuelling(
    candidates: list[Candidate],
    total_distance_miles: float,
    *,
    max_range_miles: float,
    mpg: float,
    origin_fill_max_miles: float = 50.0,
    bucket_miles: float = 1.0,
) -> FuelPlan:
    """Solve for the cheapest set of fuel stops along a measured route."""
    if total_distance_miles <= 0:
        return FuelPlan(feasible=True, total_gallons=0.0, total_cost=0.0)

    stations = collapse_to_buckets(candidates, bucket_miles)
    if not stations:
        return FuelPlan(
            feasible=False,
            reason=NO_STATIONS,
            detail={"message": "No fuel stations were found near this route."},
        )

    d = np.array([c.distance_along_miles for c in stations], dtype=np.float64)
    p = np.array([c.price for c in stations], dtype=np.float64)
    n = len(stations)
    total = float(total_distance_miles)

    # Feasibility: with stations sorted along the route, the trip is possible
    # iff no gap in the sequence [origin, stations..., destination] exceeds the
    # vehicle range.
    gaps = np.diff(np.concatenate(([0.0], d, [total])))
    worst = int(np.argmax(gaps))
    if gaps[worst] > max_range_miles:
        after = "the origin" if worst == 0 else f"mile {d[worst - 1]:.0f}"
        before = (
            "the destination" if worst == n else f"mile {d[worst]:.0f}"
        )
        return FuelPlan(
            feasible=False,
            reason=INFEASIBLE_GAP,
            detail={
                "message": (
                    f"No fuel station within {max_range_miles:.0f} miles between "
                    f"{after} and {before} (gap of {gaps[worst]:.0f} miles)."
                ),
                "gap_miles": round(float(gaps[worst]), 2),
                "gap_starts_at_mile": round(float(0.0 if worst == 0 else d[worst - 1]), 2),
                "gap_ends_at_mile": round(float(total if worst == n else d[worst]), 2),
            },
        )

    warnings: list[str] = []

    # Which stations may serve as the first stop (i.e. be reached on the fuel the
    # vehicle leaves with).
    origin_window = min(origin_fill_max_miles, max_range_miles)
    startable = d <= origin_window
    if not startable.any():
        startable = d <= max_range_miles
        warnings.append(
            f"No station within {origin_window:.0f} miles of the origin; the first "
            "leg is priced at the first reachable station instead."
        )

    per_mile = p / mpg  # dollars per mile when buying at station i
    dp = np.full(n, np.inf, dtype=np.float64)
    parent = np.full(n, -1, dtype=np.int64)

    # Base case: j is the first stop, pre-paying for the origin leg at p[j].
    dp[startable] = per_mile[startable] * d[startable]

    # A[i] = dp[i] - p[i]*d[i]/mpg, so a transition into j costs A[i] + per_mile[i]*d[j].
    intercept = np.empty(n, dtype=np.float64)
    intercept[0] = dp[0] - per_mile[0] * d[0]

    window_start = np.searchsorted(d, d - max_range_miles, side="left")

    for j in range(1, n):
        lo = int(window_start[j])
        if lo < j:
            costs = intercept[lo:j] + per_mile[lo:j] * d[j]
            k = int(np.argmin(costs))
            best = float(costs[k])
            if best < dp[j]:
                dp[j] = best
                parent[j] = lo + k
        intercept[j] = dp[j] - per_mile[j] * d[j]

    # Final leg: from the last stop to the destination.
    can_finish = (total - d) <= max_range_miles
    finish_cost = np.where(
        can_finish & np.isfinite(dp), dp + per_mile * (total - d), np.inf
    )
    if not np.isfinite(finish_cost).any():
        return FuelPlan(
            feasible=False,
            reason=INFEASIBLE_GAP,
            detail={"message": "No feasible sequence of stops covers this route."},
        )

    last = int(np.argmin(finish_cost))

    # Walk the parent chain back to the first stop.
    order: list[int] = []
    cursor = last
    while cursor != -1:
        order.append(cursor)
        cursor = int(parent[cursor])
    order.reverse()

    # Each stop buys the fuel for the leg to the next stop (or the destination).
    stops: list[FuelStop] = []
    for pos, idx in enumerate(order):
        leg_end = total if pos == len(order) - 1 else float(d[order[pos + 1]])
        gallons = (leg_end - float(d[idx])) / mpg
        if gallons <= 1e-9 and len(order) > 1:
            continue
        stops.append(
            FuelStop(
                candidate=stations[idx],
                gallons=gallons,
                cost=gallons * float(p[idx]),
            )
        )

    first = order[0]
    origin_gallons = float(d[first]) / mpg
    origin_fill = None
    if origin_gallons > 1e-9:
        origin_fill = OriginFill(
            gallons=origin_gallons,
            price=float(p[first]),
            cost=origin_gallons * float(p[first]),
            station_id=stations[first].station_id,
            distance_miles=float(d[first]),
        )

    total_gallons = sum(s.gallons for s in stops) + (
        origin_fill.gallons if origin_fill else 0.0
    )
    total_cost = sum(s.cost for s in stops) + (origin_fill.cost if origin_fill else 0.0)

    return FuelPlan(
        feasible=True,
        stops=stops,
        origin_fill=origin_fill,
        total_gallons=total_gallons,
        total_cost=total_cost,
        warnings=warnings,
    )
