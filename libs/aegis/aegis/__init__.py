"""AEGIS-ER core library.

Exposes the domain model, routing, priority scoring, assignment solving,
explainability, and a minimal in-memory world state that services can embed.
"""
from .types import (
    Incident,
    Resource,
    ResourceKind,
    ResourceStatus,
    Severity,
    Dispatch,
    DispatchState,
    EnvConditions,
    Hospital,
    LatLon,
)
from .geo import haversine_m, bearing, offset_point, point_in_circle
from .routing import RoutingEngine, HaversineRouter
from .priority import PriorityScorer
from .solver import AssignmentSolver, SolverConfig, AssignmentResult
from .world import World
from .explain import explain_dispatch

__all__ = [
    "Incident",
    "Resource",
    "ResourceKind",
    "ResourceStatus",
    "Severity",
    "Dispatch",
    "DispatchState",
    "EnvConditions",
    "Hospital",
    "LatLon",
    "haversine_m",
    "bearing",
    "offset_point",
    "point_in_circle",
    "RoutingEngine",
    "HaversineRouter",
    "PriorityScorer",
    "AssignmentSolver",
    "SolverConfig",
    "AssignmentResult",
    "World",
    "explain_dispatch",
]
