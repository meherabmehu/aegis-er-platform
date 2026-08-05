import math

import pytest

from aegis import (
    HaversineRouter,
    Incident,
    LatLon,
    PriorityScorer,
    Resource,
    ResourceKind,
    ResourceStatus,
    Severity,
    World,
    Hospital,
    EnvConditions,
)
from aegis.geo import haversine_m, bearing, offset_point
from aegis.solver import AssignmentSolver, SolverConfig


def test_haversine_known_distance():
    # ~111 km per degree latitude at equator
    a = LatLon(lat=0.0, lon=0.0)
    b = LatLon(lat=1.0, lon=0.0)
    assert 110_000 < haversine_m(a, b) < 112_000


def test_offset_roundtrip():
    a = LatLon(lat=22.7, lon=90.3)  # ~Barishal
    b = offset_point(a, 5_000.0, 90.0)  # 5 km east
    # Return
    back = offset_point(b, 5_000.0, 270.0)
    assert haversine_m(a, back) < 5.0  # sub-5-meter closure


def test_priority_scorer_mass_casualty_override():
    scorer = PriorityScorer()
    inc = Incident(
        location=LatLon(lat=23, lon=90),
        severity=Severity.CRITICAL,
        affected_count=25,
        type="collapse",
    )
    s = scorer.score(inc, local_available=5)
    assert s.final >= 0.9
    assert any("mass-casualty" in r or "large-scale" in r for r in s.reasons)


def test_solver_produces_feasible_plan():
    router = HaversineRouter()
    world = World(router=router)
    # Place 3 incidents
    for i in range(3):
        world.add_incident(Incident(
            location=LatLon(lat=23.0 + 0.01*i, lon=90.0),
            severity=Severity(3),
            affected_count=2,
            type="medical",
            region_id="default",
        ))
    # Place 5 ambulances
    for k in range(5):
        world.add_resource(Resource(
            kind=ResourceKind.AMBULANCE,
            name=f"AMB-{k}",
            home_base=LatLon(lat=23.0, lon=90.0 + 0.01*k),
            location=LatLon(lat=23.0, lon=90.0 + 0.01*k),
        ))
    world.add_hospital(Hospital(
        name="Test Hospital", location=LatLon(lat=23.0, lon=90.02),
        total_beds=20, available_beds=20,
    ))
    dispatches = world.plan()
    assert len(dispatches) >= 3  # every medical incident gets at least one ambulance
    # No double-booking
    res_ids = [d.resource_id for d in dispatches]
    assert len(res_ids) == len(set(res_ids))
    # All dispatches feasible (ETA >= 0; co-located resources legitimately have 0)
    for d in dispatches:
        assert d.eta_seconds >= 0


def test_solver_respects_resource_failure():
    router = HaversineRouter()
    world = World(router=router)
    world.add_incident(Incident(
        location=LatLon(lat=23.0, lon=90.0),
        severity=Severity.SEVERE,
        affected_count=1,
    ))
    r1 = world.add_resource(Resource(
        kind=ResourceKind.AMBULANCE, name="AMB-OK",
        home_base=LatLon(lat=23.001, lon=90.0),
        location=LatLon(lat=23.001, lon=90.0),
    ))
    world.add_resource(Resource(
        kind=ResourceKind.AMBULANCE, name="AMB-BROKEN",
        home_base=LatLon(lat=22.999, lon=90.0),
        location=LatLon(lat=22.999, lon=90.0),
        status=ResourceStatus.FAILED,
    ))
    dispatches = world.plan()
    assert len(dispatches) == 1
    assert dispatches[0].resource_id == r1.resource_id  # chose the working one


def test_simulation_loop_completes_incident():
    router = HaversineRouter()
    world = World(router=router)
    world.add_incident(Incident(
        location=LatLon(lat=23.0, lon=90.0),
        severity=Severity.MAJOR,
        affected_count=1,
        time_sensitivity_min=10,
    ))
    world.add_resource(Resource(
        kind=ResourceKind.AMBULANCE, name="AMB-1",
        home_base=LatLon(lat=23.005, lon=90.0),
        location=LatLon(lat=23.005, lon=90.0),
        speed_kmh=60,
    ))
    world.add_hospital(Hospital(name="H", location=LatLon(lat=23.0, lon=90.01),
                                total_beds=10, available_beds=10))
    world.plan()
    # Fast-forward 20 minutes of simulation (big dt)
    for _ in range(200):
        world.advance(dt_seconds=20.0)
    resolved = [i for i in world.incidents.values() if i.status == "RESOLVED"]
    assert len(resolved) >= 1


def test_road_closure_slowdown():
    r = HaversineRouter()
    a = LatLon(lat=0, lon=0); b = LatLon(lat=0.1, lon=0)
    good = r.route(a, b, 60.0, EnvConditions())
    bad = r.route(a, b, 60.0, EnvConditions(road_status="closed", weather="storm"))
    assert bad.eta_seconds > good.eta_seconds * 1.5
