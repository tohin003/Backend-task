"""Geometry helpers: distances, polyline measurement, simplification, projection.

Everything here is vectorised with NumPy. The one non-obvious trick is that we
project lat/lon onto the 3D unit sphere so a Euclidean (chord) KD-tree gives us
true great-circle nearest neighbours, which is what makes corridor matching fast
enough to do on every request.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

EARTH_RADIUS_MILES = 3958.7613


def to_unit_vectors(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Convert degree lat/lon arrays to (N, 3) unit vectors on the sphere."""
    lat_r = np.radians(np.asarray(lat, dtype=np.float64))
    lon_r = np.radians(np.asarray(lon, dtype=np.float64))
    cos_lat = np.cos(lat_r)
    return np.column_stack(
        (cos_lat * np.cos(lon_r), cos_lat * np.sin(lon_r), np.sin(lat_r))
    )


def miles_to_chord(miles: float) -> float:
    """Great-circle distance in miles -> chord length on the unit sphere."""
    return 2.0 * np.sin(min(miles / EARTH_RADIUS_MILES, np.pi) / 2.0)


def chord_to_miles(chord: np.ndarray | float) -> np.ndarray | float:
    """Chord length on the unit sphere -> great-circle distance in miles."""
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0, 1))


def haversine_miles(
    lat1: float | np.ndarray,
    lon1: float | np.ndarray,
    lat2: float | np.ndarray,
    lon2: float | np.ndarray,
) -> float | np.ndarray:
    """Great-circle distance in miles between two points (or arrays of points)."""
    lat1_r, lon1_r, lat2_r, lon2_r = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def segment_lengths_miles(coords: np.ndarray) -> np.ndarray:
    """Length in miles of each consecutive segment of an (N, 2) lon/lat array."""
    if len(coords) < 2:
        return np.zeros(0, dtype=np.float64)
    lon = coords[:, 0]
    lat = coords[:, 1]
    return haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])


def cumulative_miles(coords: np.ndarray) -> np.ndarray:
    """Cumulative distance in miles at each vertex of an (N, 2) lon/lat array."""
    if len(coords) == 0:
        return np.zeros(0, dtype=np.float64)
    out = np.zeros(len(coords), dtype=np.float64)
    if len(coords) > 1:
        np.cumsum(segment_lengths_miles(coords), out=out[1:])
    return out


def simplify_polyline(coords: np.ndarray, tolerance_deg: float = 0.002) -> np.ndarray:
    """Iterative Ramer-Douglas-Peucker simplification.

    Used only to shrink the geometry we hand back in the JSON payload; the full
    resolution polyline is what we measure and project against internally.
    """
    n = len(coords)
    if n <= 2 or tolerance_deg <= 0:
        return coords

    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        seg = coords[start + 1 : end]
        a = coords[start]
        b = coords[end]
        ab = b - a
        denom = float(ab[0] ** 2 + ab[1] ** 2)
        if denom == 0.0:
            dist = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            # Perpendicular distance from each interior point to the chord a->b.
            dist = np.abs(
                ab[0] * (a[1] - seg[:, 1]) - (a[0] - seg[:, 0]) * ab[1]
            ) / np.sqrt(denom)
        idx = int(np.argmax(dist))
        if dist[idx] > tolerance_deg:
            split = start + 1 + idx
            keep[split] = True
            stack.append((start, split))
            stack.append((split, end))

    return coords[keep]


def subsample_for_index(
    coords: np.ndarray, cumulative: np.ndarray, spacing_miles: float, max_points: int
) -> np.ndarray:
    """Pick vertex indices spaced roughly ``spacing_miles`` apart along the route.

    Keeps the spatial index small without meaningfully degrading how accurately
    we can locate a station along the route.
    """
    n = len(coords)
    if n <= 2:
        return np.arange(n)

    total = float(cumulative[-1])
    if total <= 0:
        return np.arange(n)

    target_count = int(total / spacing_miles) + 1
    target_count = max(2, min(target_count, max_points, n))
    targets = np.linspace(0.0, total, target_count)
    idx = np.unique(np.searchsorted(cumulative, targets).clip(0, n - 1))
    if idx[0] != 0:
        idx = np.concatenate(([0], idx))
    if idx[-1] != n - 1:
        idx = np.concatenate((idx, [n - 1]))
    return idx


@dataclass(slots=True)
class RouteGeometry:
    """A measured route: raw geometry plus a spatial index over its vertices."""

    coords: np.ndarray  # (N, 2) lon/lat
    cumulative: np.ndarray  # (N,) miles from origin at each vertex
    index_positions: np.ndarray  # (M,) indices of the subsampled vertices
    tree: cKDTree  # KD-tree over the subsampled vertices (unit vectors)

    @property
    def total_miles(self) -> float:
        return float(self.cumulative[-1]) if len(self.cumulative) else 0.0

    @classmethod
    def build(
        cls,
        coords: np.ndarray,
        index_spacing_miles: float = 0.5,
        max_index_points: int = 20000,
        total_miles: float | None = None,
    ) -> "RouteGeometry":
        coords = np.asarray(coords, dtype=np.float64)
        cumulative = cumulative_miles(coords)
        # Great-circle summation over the polyline is very slightly shorter than
        # the road distance the router reports. Rescale so a station's
        # "distance along route" is on exactly the same scale as the total.
        if total_miles and len(cumulative) and cumulative[-1] > 0:
            cumulative = cumulative * (total_miles / cumulative[-1])
        positions = subsample_for_index(
            coords, cumulative, index_spacing_miles, max_index_points
        )
        sampled = coords[positions]
        tree = cKDTree(to_unit_vectors(sampled[:, 1], sampled[:, 0]))
        return cls(
            coords=coords, cumulative=cumulative, index_positions=positions, tree=tree
        )

    def bounds(self) -> dict[str, float]:
        lon = self.coords[:, 0]
        lat = self.coords[:, 1]
        return {
            "min_lon": float(lon.min()),
            "min_lat": float(lat.min()),
            "max_lon": float(lon.max()),
            "max_lat": float(lat.max()),
        }

    def locate(
        self, lat: np.ndarray, lon: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Locate points relative to the route.

        Returns ``(offset_miles, distance_along_route_miles)`` for every input
        point, computed as a single batched nearest-neighbour query.
        """
        if len(lat) == 0:
            return np.zeros(0), np.zeros(0)
        chord, nearest = self.tree.query(to_unit_vectors(lat, lon), k=1, workers=-1)
        offset = chord_to_miles(chord)
        along = self.cumulative[self.index_positions[nearest]]
        return np.asarray(offset, dtype=np.float64), np.asarray(along, dtype=np.float64)
