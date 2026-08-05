"""In-memory world state used by services/simulator.

Provides a thread-safe substrate for the MVP. In production this is replaced
by Postgres+Redis+Kafka; the Python object model is identical, which keeps the
solver and services portable across deployment modes (edge-embedded, demo,
full cloud).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

from .routing import HaversineRouter, Route, RoutingEngine, estimate_route_for_resource
from .solver import AssignmentSolver, SolverConfig
from .priority import PriorityScorer
from .types import (
    Dispatch,
    DispatchState,
    Hospital,
    Incident,
    LatLon,
    Resource,
    ResourceKind,
    ResourceStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class World:
    """Mutable world state with a tick-based simulation loop."""

    def __init__(self, router: Optional[RoutingEngine] = None,
                 solver_config: Optional[SolverConfig] = None):
        self._lock = threading.RLock()
        self.incidents: dict[str, Incident] = {}
        self.resources: dict[str, Resource] = {}
        self.hospitals: dict[str, Hospital] = {}
        self.dispatches: dict[str, Dispatch] = {}
        self.router = router or HaversineRouter()
        self.scorer = PriorityScorer()
        self.solver = AssignmentSolver(config=solver_config, router=self.router, scorer=self.scorer)
        self._listeners: list[Callable[[str, object], None]] = []
        self.tick = 0
        self.sim_clock: float = 0.0  # seconds since world creation; used by advance()
        self._event_log: list[tuple[int, str, dict]] = []
        self._dispatch_progress: dict[str, dict] = {}  # resource_id → progress state

    # ----- eventing -----
    def add_listener(self, fn: Callable[[str, object], None]):
        self._listeners.append(fn)

    def _emit(self, evt_type: str, payload) -> None:
        with self._lock:
            self.tick += 1
            self._event_log.append((self.tick, evt_type, {"id": getattr(payload, "incident_id", getattr(payload, "resource_id", getattr(payload, "dispatch_id", None)))}))
        for fn in list(self._listeners):
            try:
                fn(evt_type, payload)
            except Exception:
                pass

    # ----- adders -----
    def add_incident(self, inc: Incident) -> Incident:
        with self._lock:
            seconds_elapsed = 0.0
            local = sum(1 for r in self.resources.values() if r.status == ResourceStatus.AVAILABLE)
            score = self.scorer.score(inc, seconds_elapsed=seconds_elapsed, local_available=local)
            inc.urgency_score = score.final
            self.incidents[inc.incident_id] = inc
        self._emit("incident.created", inc)
        return inc

    def add_resource(self, res: Resource) -> Resource:
        with self._lock:
            self.resources[res.resource_id] = res
        self._emit("resource.registered", res)
        return res

    def add_hospital(self, h: Hospital) -> Hospital:
        with self._lock:
            self.hospitals[h.hospital_id] = h
        self._emit("hospital.registered", h)
        return h

    # ----- status updates -----
    def update_resource_location(self, resource_id: str, loc: LatLon, status: Optional[ResourceStatus] = None):
        with self._lock:
            r = self.resources.get(resource_id)
            if not r:
                return
            r.location = loc
            r.location_updated = _utcnow()
            if status is not None:
                r.status = status
        self._emit("resource.moved", r)

    def set_resource_status(self, resource_id: str, status: ResourceStatus):
        with self._lock:
            r = self.resources.get(resource_id)
            if not r:
                return
            r.status = status
            r.version += 1
        self._emit("resource.status_changed", r)

    def set_hospital_capacity(self, hospital_id: str, available_beds: int):
        with self._lock:
            h = self.hospitals.get(hospital_id)
            if not h:
                return
            h.available_beds = max(0, min(h.total_beds, available_beds))
        self._emit("hospital.updated", h)

    def close_road_near(self, *_):
        # Hook for environment events; MVP just affects router traffic multiplier via incidents' env
        pass

    # ----- core planning -----
    def plan(self) -> list[Dispatch]:
        """Run one planning cycle: score, solve, commit."""
        with self._lock:
            incidents = [i for i in self.incidents.values() if i.status not in ("RESOLVED", "CANCELLED")]
            resources = list(self.resources.values())
            hospitals = list(self.hospitals.values())
            # Tell the solver which resources/incidents are ALREADY covered by
            # an active dispatch by temporarily marking those resources as
            # DISPATCHED — otherwise it sees them as AVAILABLE every tick and
            # proposes duplicate overlapping assignments.
            covered_res = {d.resource_id for d in self.dispatches.values()
                           if d.state not in (DispatchState.COMPLETED, DispatchState.REJECTED)}
            saved_status: dict[str, object] = {}
            for rid in covered_res:
                r = self.resources.get(rid)
                if r and r.status == ResourceStatus.AVAILABLE:
                    saved_status[rid] = r.status
                    r.status = ResourceStatus.DISPATCHED
            # Also mark incidents already in RESPONDING/TRANSPORTING etc as
            # not needing a fresh dispatch for the greedy — we do this by
            # excluding them from the planning input.
            plan_incidents = [i for i in incidents
                              if i.status == "REPORTED"
                              and not any(d.incident_id == i.incident_id
                                          for d in self.dispatches.values()
                                          if d.state not in (DispatchState.COMPLETED, DispatchState.REJECTED))]
            if not plan_incidents:
                return []
            try:
                result = self.solver.solve(plan_incidents, resources, hospitals=hospitals)
            finally:
                for rid, st in saved_status.items():
                    self.resources[rid].status = st

        # Commit proposed dispatches, avoiding double-booking currently-en-route
        new_dispatches: list[Dispatch] = []
        with self._lock:
            # A resource is "booked" if it has any COMPLETED/REJECTED cleared
            # active dispatch. We track the canonical dispatch per resource.
            active_states = {
                DispatchState.PROPOSED, DispatchState.ACCEPTED,
                DispatchState.EN_ROUTE, DispatchState.ON_SCENE,
                DispatchState.TRANSPORTING, DispatchState.REROUTED,
            }
            # Build canonical active-dispatch per resource_id and per
            # (incident_id, resource_id) pair. Replacing an existing dispatch
            # for a resource with a new plan is done below.
            active_by_resource: dict[str, Dispatch] = {}
            active_pair: dict[tuple[str, str], Dispatch] = {}
            stale_ids: set[str] = set()
            for d in self.dispatches.values():
                if d.state in active_states:
                    # If this resource/pair already has an active dispatch,
                    # the older row is stale (can happen if replan generates
                    # a new dispatch_id for the same moving unit).
                    if d.resource_id in active_by_resource:
                        stale_ids.add(d.dispatch_id)
                        continue
                    if (d.incident_id, d.resource_id) in active_pair:
                        stale_ids.add(d.dispatch_id)
                        continue
                    active_by_resource[d.resource_id] = d
                    active_pair[(d.incident_id, d.resource_id)] = d
            # Drop stale duplicate rows
            for sid in stale_ids:
                self.dispatches.pop(sid, None)

            for d in result.dispatches:
                existing = active_by_resource.get(d.resource_id)
                if existing is not None:
                    # Resource already actively dispatched. If it's to the
                    # same incident, keep the existing dispatch (don't churn
                    # IDs which resets the progress timer). If it's to a
                    # DIFFERENT incident, refuse to preempt — mid-mission
                    # reassignment is a future feature (priority inversion).
                    continue
                if (d.incident_id, d.resource_id) in active_pair:
                    # Same pair already covered; skip.
                    continue
                # Mark resource
                r = self.resources.get(d.resource_id)
                if not r:
                    continue
                if r.status in (ResourceStatus.FAILED, ResourceStatus.MAINTENANCE):
                    continue
                if r.status == ResourceStatus.AVAILABLE:
                    r.status = ResourceStatus.DISPATCHED
                r.current_incident_id = d.incident_id
                r.dispatch_id = d.dispatch_id
                r.version += 1
                # Immediately mark as EN_ROUTE so advance() picks it up.
                d.state = DispatchState.EN_ROUTE
                self.dispatches[d.dispatch_id] = d
                self._dispatch_progress[d.resource_id] = {
                    "phase": "en_route",
                    "started": self.sim_clock,
                    "from_loc": (r.location.lat, r.location.lon),
                    "eta_seconds": d.eta_seconds,
                    "incident_id": d.incident_id,
                    "dispatch_id": d.dispatch_id,
                }
                active_by_resource[d.resource_id] = d
                active_pair[(d.incident_id, d.resource_id)] = d
                new_dispatches.append(d)
        for d in new_dispatches:
            self._emit("dispatch.proposed", d)
        return new_dispatches

    # ----- simulation / execution -----
    def advance(self, dt_seconds: float = 1.0):
        """Advance the simulated world by dt seconds — moves resources along routes, completes dispatches.

        Uses simulation-time (self.sim_clock), not wall-clock, so tests and
        simulators can run arbitrarily fast.
        """
        with self._lock:
            self.sim_clock += dt_seconds
            now = self.sim_clock
            to_complete = []
            to_scene = []
            to_transport = []

            # Periodically garbage-collect fully-COMPLETED/REJECTED dispatches
            # so they don't accumulate forever and bloat snapshots.
            if self.tick % 200 == 0:
                done = [did for did, d in self.dispatches.items()
                        if d.state in (DispatchState.COMPLETED, DispatchState.REJECTED)]
                for did in done:
                    self.dispatches.pop(did, None)

            # Purge stale progress entries first: if the dispatch is already
            # COMPLETED/REJECTED, or the incident is already RESOLVED, the
            # leftover entry must NOT be advanced (it would re-set inc.status
            # to RESPONDING and "un-resolve" a finished incident).
            for res_id, prog in list(self._dispatch_progress.items()):
                r = self.resources.get(res_id)
                d = self.dispatches.get(prog["dispatch_id"])
                inc = self.incidents.get(prog["incident_id"])
                if not r or not d or not inc:
                    self._dispatch_progress.pop(res_id, None)
                    continue
                if d.state in (DispatchState.COMPLETED, DispatchState.REJECTED):
                    self._dispatch_progress.pop(res_id, None)
                    continue
                if inc.status in ("RESOLVED", "CANCELLED"):
                    self._dispatch_progress.pop(res_id, None)
                    # Make sure resource isn't stuck DISPATCHED
                    if r.status not in (ResourceStatus.FAILED, ResourceStatus.MAINTENANCE):
                        r.status = ResourceStatus.AVAILABLE
                        r.current_incident_id = None
                        r.dispatch_id = None
                    continue
                phase = prog["phase"]
                if phase == "en_route":
                    elapsed = now - prog["started"]
                    frac = min(1.0, elapsed / max(1.0, prog["eta_seconds"]))
                    # Move smoothly from the dispatch origin (captured at
                    # plan-time, not the current r.location which drifts each
                    # tick) to the incident via great-circle interpolation.
                    # Using the current location as the start caused the unit
                    # to re-target every tick and never actually "arrive"
                    # under edge-case timing.
                    from .geo import haversine_m, offset_point, bearing
                    start_lat, start_lon = prog["from_loc"]
                    start_loc = LatLon(lat=start_lat, lon=start_lon)
                    target_loc = inc.location
                    seg_len = haversine_m(start_loc, target_loc)
                    if seg_len > 1 and frac < 1.0:
                        move = seg_len * frac
                        br = bearing(start_loc, target_loc)
                        r.location = offset_point(start_loc, move, br)
                    if frac >= 1.0:
                        r.status = ResourceStatus.ON_SCENE
                        d.state = DispatchState.ON_SCENE
                        d.eta_seconds = 0
                        r.location = inc.location
                        inc.status = "RESPONDING"
                        prog["phase"] = "on_scene"
                        prog["scene_started"] = now
                        to_scene.append((d, inc))
                elif phase == "on_scene":
                    elapsed = now - prog.get("scene_started", now)
                    # Quick scene time for the demo — short enough that
                    # judges see resolution in ~15-20s of demo-time.
                    scene_time = 8 + inc.affected_count * 3
                    if elapsed >= scene_time:
                        # For non-medical incidents (fire/collapse/flood/rescue)
                        # there's no hospital leg — resolve immediately after
                        # on-scene time. For medical/crash, transport to hospital.
                        is_medical = r.kind == ResourceKind.AMBULANCE and inc.type in ("medical", "crash", "collapse")
                        if is_medical and self.hospitals:
                            best = None
                            best_d = float("inf")
                            for h in self.hospitals.values():
                                if h.available_beds <= 0:
                                    continue
                                dd = estimate_route_for_resource(r, h.location, inc.env, self.router).distance_m
                                if dd < best_d:
                                    best_d = dd
                                    best = h
                            if best is not None:
                                d.target_hospital_id = best.hospital_id
                                rt = self.router.route(inc.location, best.location, r.effective_speed_kmh(), inc.env)
                                d.route = rt.path
                                d.eta_seconds = rt.eta_seconds
                                prog["phase"] = "transporting"
                                prog["started"] = now
                                prog["from_loc"] = (inc.location.lat, inc.location.lon)
                                prog["hospital_id"] = best.hospital_id
                                r.status = ResourceStatus.TRANSPORTING
                                d.state = DispatchState.TRANSPORTING
                                to_transport.append((d, inc, best))
                                continue
                        self._complete_dispatch(d, r, inc, prog, to_complete)
                elif phase == "transporting":
                    elapsed = now - prog["started"]
                    frac = min(1.0, elapsed / max(1.0, d.eta_seconds))
                    from .geo import haversine_m, offset_point, bearing
                    tgt_hosp = self.hospitals.get(prog.get("hospital_id"))
                    if tgt_hosp and frac < 1.0:
                        # Smooth interpolate from the hospital-leg origin
                        # (snapshot at on_scene→transporting transition) to
                        # the target hospital — same from_loc pattern as
                        # en_route so the unit reliably arrives.
                        fl = prog.get("from_loc")
                        if fl:
                            start_loc = LatLon(lat=fl[0], lon=fl[1])
                        else:
                            start_loc = inc.location
                        seg_len = haversine_m(start_loc, tgt_hosp.location)
                        if seg_len > 1:
                            move = seg_len * frac
                            br = bearing(start_loc, tgt_hosp.location)
                            r.location = offset_point(start_loc, move, br)
                    if frac >= 1.0:
                        h = self.hospitals.get(prog.get("hospital_id"))
                        if h and h.available_beds > 0:
                            h.available_beds = max(0, h.available_beds - 1)
                        self._complete_dispatch(d, r, inc, prog, to_complete)

        # Emit outside the lock to avoid recursive lock issues
        for d, inc in to_scene:
            inc.status = "RESPONDING"
            self._emit("dispatch.on_scene", d)
        for d, inc, h in to_transport:
            self._emit("dispatch.transporting", d)
        for d, inc, r in to_complete:
            self._emit("dispatch.completed", d)
            self._emit("incident.resolved", inc)

    def _complete_dispatch(self, d: Dispatch, r: Resource, inc: Incident, prog, bucket):
        r.status = ResourceStatus.AVAILABLE
        r.current_incident_id = None
        r.dispatch_id = None
        r.version += 1
        d.state = DispatchState.COMPLETED
        inc.status = "RESOLVED"
        inc.resolved_at = _utcnow()
        self._dispatch_progress.pop(r.resource_id, None)
        # Return resource toward home base? We leave at hospital/incident location (realistic)
        bucket.append((d, inc, r))

    # ----- snapshots -----
    def snapshot(self) -> dict:
        with self._lock:
            from .types import ResourceKind
            deployable = [r for r in self.resources.values()
                          if r.kind not in (ResourceKind.HOSPITAL, ResourceKind.EOC)]
            active_deployed = [r for r in deployable
                               if r.status in (ResourceStatus.DISPATCHED, ResourceStatus.ON_SCENE, ResourceStatus.TRANSPORTING)]
            util = round(len(active_deployed) / max(1, len(deployable)), 3)
            return {
                "tick": self.tick,
                "incidents": [i.model_dump(mode="json") for i in self.incidents.values()],
                "resources": [r.model_dump(mode="json") for r in self.resources.values()],
                "hospitals": [h.model_dump(mode="json") for h in self.hospitals.values()],
                "dispatches": [d.model_dump(mode="json") for d in self.dispatches.values()],
                "active_count": sum(1 for i in self.incidents.values() if i.status not in ("RESOLVED", "CANCELLED")),
                "available_resources": sum(1 for r in deployable if r.status == ResourceStatus.AVAILABLE),
                "utilization": util,
                "mean_eta_seconds": self._mean_eta(),
            }

    def _mean_eta(self) -> float:
        active = [d for d in self.dispatches.values() if d.state in (DispatchState.PROPOSED, DispatchState.ACCEPTED, DispatchState.EN_ROUTE)]
        if not active:
            return 0.0
        return round(sum(d.eta_seconds for d in active) / len(active), 1)
