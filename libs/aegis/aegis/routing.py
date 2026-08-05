"""Routing and ETA estimation.

The real deployment plugs Valhalla/OSRM behind this interface; for portability
and tests we ship a HaversineRouter that applies per-road-class speeds and an
environmental traffic multiplier, which is accurate enough for dispatch-grade
decisions when a live road graph is unavailable.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from .geo import haversine_m, offset_point, bearing
from .types import EnvConditions, LatLon, Resource, ResourceKind


ROUTE_STRAIGHTNESS = 1.25  # urban roads aren't great-circle; inflate distance


@dataclass
class Route:
    origin: LatLon
    destination: LatLon
    distance_m: float
    eta_seconds: float
    path: list[LatLon] = field(default_factory=list)


class RoutingEngine:
    """Abstract routing engine; subclass to plug OSRM/Valhalla/Google Maps."""

    def route(self, origin: LatLon, dest: LatLon, speed_kmh: float, env: Optional[EnvConditions] = None) -> Route:  # pragma: no cover - interface
        raise NotImplementedError

    def bulk_eta(self, reqs: list[tuple[LatLon, LatLon, float, Optional[EnvConditions]]]) -> list[Route]:
        return [self.route(o, d, s, e) for (o, d, s, e) in reqs]


class HaversineRouter(RoutingEngine):
    """Fall-back router with traffic/weather multiplier and gentle jitter."""

    def __init__(self, seed: int = 42, jitter: float = 0.08):
        self._rng = random.Random(seed)
        self.jitter = jitter

    def route(self, origin: LatLon, dest: LatLon, speed_kmh: float, env: Optional[EnvConditions] = None) -> Route:
        env = env or EnvConditions()
        base_m = haversine_m(origin, dest)
        if base_m < 5:
            return Route(origin, dest, 0.0, 0.0, [origin, dest])
        # Helicopters fly straight; ground vehicles follow roads.
        straight_dist = base_m
        # Build a plausible path of 4-8 points
        n = min(8, max(4, int(straight_dist // 1500)))
        path: list[LatLon] = [origin]
        brng = bearing(origin, dest)
        step = straight_dist / n
        cur = origin
        for i in range(1, n):
            jitter_b = brng + self._rng.uniform(-12, 12)
            jitter_d = step * (1 + self._rng.uniform(-0.1, 0.1))
            cur = offset_point(cur, jitter_d, jitter_b)
            path.append(cur)
        path.append(dest)
        # Accumulate path distance (sum of segment haversines)
        distance_m = 0.0
        for a, b in zip(path, path[1:]):
            distance_m += haversine_m(a, b)

        speed_ms = max(1.0, (speed_kmh * 1000.0 / 3600.0))
        mult = env.traffic_multiplier()
        eta = distance_m / speed_ms / max(0.05, mult)
        eta *= (1.0 + self._rng.uniform(-self.jitter, self.jitter))
        return Route(origin, dest, distance_m, max(1.0, eta), path)


def estimate_route_for_resource(res: Resource, dest: LatLon, env: Optional[EnvConditions] = None, router: Optional[RoutingEngine] = None) -> Route:
    router = router or HaversineRouter()
    if res.kind == ResourceKind.HELICOPTER:
        # Helicopters mostly fly straight, faster, partially weather-dependent
        env_eff = EnvConditions(
            weather=env.weather if env else "clear",
            wind_kmh=env.wind_kmh if env else 0,
            visibility_m=env.visibility_m if env else 5000,
            road_status="open",
        )
        r = router.route(res.location, dest, res.effective_speed_kmh(), env_eff)
        # Straighten path for air
        r.path = [r.origin, r.destination]
        r.distance_m = haversine_m(r.origin, r.destination)
        r.eta_seconds = r.distance_m / max(1.0, res.effective_speed_kmh() * 1000 / 3600) / max(0.2, env_eff.traffic_multiplier())
        return r
    return router.route(res.location, dest, res.effective_speed_kmh(), env)
