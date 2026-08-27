"""Decoder for the encoded-polyline format used by OSRM/Google.

Requesting ``geometries=polyline6`` instead of ``geojson`` cuts the routing
response for a coast-to-coast trip from ~276 KB to ~118 KB and, in practice,
takes 3-4x less wall-clock time on the public OSRM server - for byte-identical
geometry. Decoding is vectorised so it costs about a millisecond.
"""

from __future__ import annotations

import numpy as np


def decode(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode an encoded polyline into an (N, 2) array of ``[lon, lat]``.

    The format stores zig-zag encoded deltas in 5-bit chunks, each chunk offset
    by 63 and carrying a continuation flag in bit 6.
    """
    if not encoded:
        return np.zeros((0, 2), dtype=np.float64)

    raw = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    if raw.size == 0:
        return np.zeros((0, 2), dtype=np.float64)

    chunk = raw & 0x1F
    is_last = (raw & 0x20) == 0

    # Chunks belong to the value that ends at the next `is_last` position.
    value_id = np.concatenate(([0], np.cumsum(is_last)[:-1]))
    n_values = int(value_id[-1]) + 1

    # Position of each chunk within its value, so we know how far to shift it.
    starts = np.searchsorted(value_id, np.arange(n_values))
    shift = (np.arange(raw.size) - starts[value_id]) * 5

    contribution = chunk << shift
    combined = np.zeros(n_values, dtype=np.int64)
    np.add.at(combined, value_id, contribution)

    # Undo zig-zag: even -> +n/2, odd -> ~(n/2).
    deltas = np.where(combined & 1, ~(combined >> 1), combined >> 1)

    if deltas.size % 2:  # malformed tail; drop it rather than misalign pairs
        deltas = deltas[:-1]

    coords = np.cumsum(deltas.reshape(-1, 2), axis=0) / (10.0**precision)
    # Encoded polylines are lat,lon; GeoJSON order is lon,lat.
    return np.column_stack((coords[:, 1], coords[:, 0]))
