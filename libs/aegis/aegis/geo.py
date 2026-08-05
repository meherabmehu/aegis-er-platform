"""Geospatial primitives used by routing, solver, and simulator.

All distances are in meters unless noted. Earth is modeled as a sphere of
radius 6371.0088 km (mean radius) — sufficient for dispatch-grade ETAs given
the other sources of uncertainty (traffic, weather). We deliberately avoid a
heavy dependency like GEOS here so the core library stays pure-Python/numpy.
"""
from __future__ import annotations

import math
from typing import Iterable

from .types import LatLon

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance in meters between two points."""
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bearing(a: LatLon, b: LatLon) -> float:
    """Initial bearing (degrees, 0..360) from point a to point b."""
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset_point(p: LatLon, distance_m: float, degrees_bearing: float) -> LatLon:
    """Return a new LatLon `distance_m` away on the given bearing."""
    if distance_m <= 0:
        return p
    d = distance_m / EARTH_RADIUS_M
    brng = math.radians(degrees_bearing)
    lat1 = math.radians(p.lat)
    lon1 = math.radians(p.lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return LatLon(lat=math.degrees(lat2), lon=math.degrees(lon2))


def point_in_circle(p: LatLon, center: LatLon, radius_m: float) -> bool:
    return haversine_m(p, center) <= radius_m


def nearest_k(point: LatLon, candidates: Iterable, k: int, key=lambda c: c.location):
    """Return the k nearest candidates to `point`, sorted ascending by distance."""
    scored = []
    for c in candidates:
        loc = key(c)
        scored.append((haversine_m(point, loc), c))
    scored.sort(key=lambda x: x[0])
    return scored[:k]
