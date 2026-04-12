"""Pure functions for computing GPX track statistics."""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

_EARTH_RADIUS_M = 6_371_000.0


def calculate_haversine_distance_m(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Return the great-circle distance in metres between two WGS-84 points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return float(2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a)))


def calculate_track_distance_m(points: Iterable[Any]) -> float:
    """Return total track distance in metres by summing Haversine segments.

    Each element must expose ``latitude`` and ``longitude`` attributes.
    Points with ``None`` lat/lon are silently skipped.
    """
    total = 0.0
    prev_lat: float | None = None
    prev_lon: float | None = None

    for p in points:
        lat = getattr(p, "latitude", None)
        lon = getattr(p, "longitude", None)
        if lat is None or lon is None:
            prev_lat = prev_lon = None
            continue
        if prev_lat is not None and prev_lon is not None:
            total += calculate_haversine_distance_m(prev_lat, prev_lon, float(lat), float(lon))
        prev_lat = float(lat)
        prev_lon = float(lon)

    return total


def calculate_elevation_gain_m(points: Iterable[Any]) -> float:
    """Return cumulative elevation gain in metres (sum of positive ascents only).

    Each element must expose an ``elevation`` attribute (metres, or ``None``).
    Consecutive pairs where either point has ``None`` elevation are skipped.
    """
    total = 0.0
    prev_elev: float | None = None

    for p in points:
        elev = getattr(p, "elevation", None)
        if elev is None:
            prev_elev = None
            continue
        elev = float(elev)
        if prev_elev is not None:
            delta = elev - prev_elev
            if delta > 0:
                total += delta
        prev_elev = elev

    return total
