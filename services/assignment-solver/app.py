"""AEGIS-ER API service.

Single-process service that exposes:
  - REST endpoints for CRUD on incidents, resources, hospitals, dispatches
  - WebSocket `/ws` streaming of world snapshots for dashboards
  - Built-in solver loop (runs every reoptimize_interval seconds and on events)
  - Disaster simulator hooks (auto-generate incidents/resources when enabled)

Designed to run as a single container for MVP; in production each logical
service is split out and communicates over Kafka, per the architecture doc.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aegis import (
    EnvConditions,
    HaversineRouter,
    Hospital,
    Incident,
    LatLon,
    Resource,
    ResourceKind,
    ResourceStatus,
    Severity,
    World,
)
from aegis.solver import SolverConfig


# ---------- Configuration ----------
SIM_DEFAULT = os.environ.get("AEGIS_SIMULATOR", "false").lower() == "true"
TICK_MS = int(os.environ.get("AEGIS_TICK_MS", "250"))
SIM_SPEED = float(os.environ.get("AEGIS_SIM_SPEED", "120"))
REOPTIMIZE_MS = int(os.environ.get("AEGIS_REOPTIMIZE_MS", "500"))
SCENARIO = os.environ.get("AEGIS_SCENARIO", "bangladesh")
SOLVER_BUDGET_MS = int(os.environ.get("AEGIS_SOLVER_BUDGET_MS", "200"))


# ---------- World ----------
router = HaversineRouter(seed=int(time.time()) % 2**32)
solver_config = SolverConfig(time_budget_ms=SOLVER_BUDGET_MS)
world = World(router=router, solver_config=solver_config)

# BD hub coordinates shared between bootstrap and simulator spawn logic.
HUBS = [
    (23.8103, 90.4125, "Dhaka"),
    (22.7010, 90.3535, "Barishal"),
    (22.3569, 91.7832, "Chattogram"),
    (24.3636, 88.6241, "Rajshahi"),
    (22.8456, 89.5403, "Khulna"),
    (24.9045, 91.8611, "Sylhet"),
    (25.7439, 89.2752, "Rangpur"),
    (23.4607, 91.1809, "Cumilla"),
]


# ---------- Pydantic wire models ----------
class LatLonIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class IncidentIn(BaseModel):
    type: str = "medical"
    location: LatLonIn
    severity: int = 3
    affected_count: int = 1
    time_sensitivity_min: int = 10
    notes: str = ""
    region_id: str = "default"
    weather: str = "clear"
    road_status: str = "open"
    hazard: Optional[str] = None


class ResourceIn(BaseModel):
    kind: str
    name: Optional[str] = None
    home_base: LatLonIn
    location: Optional[LatLonIn] = None
    crew_count: int = 2
    speed_kmh: float = 0.0


class HospitalIn(BaseModel):
    name: str
    location: LatLonIn
    total_beds: int = 50
    available_beds: int = 50
    has_trauma: bool = True
    has_burn_unit: bool = False


class EnvEventIn(BaseModel):
    kind: str  # road_close | hospital_full | resource_fail | weather
    target_id: Optional[str] = None
    location: Optional[LatLonIn] = None
    value: Optional[str] = None


class ActionIn(BaseModel):
    action: str  # plan | reset | sim_start | sim_stop | tick
    steps: Optional[int] = 1


# ---------- Helpers ----------
def _latlon(p: LatLonIn) -> LatLon:
    return LatLon(lat=p.lat, lon=p.lon)


# ---------- Bootstrap a default country-sized scenario ----------
def _bootstrap_default_world(w: World, scenario: str = "bangladesh"):
    """Seed a demonstration world with cities, hospitals, and resources."""
    rng = random.Random(7)
    # Approximate regional hubs (lat, lon, name)
    if scenario == "bangladesh":
        hubs = list(HUBS)
    else:
        hubs = [(23.0 + 0.2*i, 90.0 + 0.2*i, f"Hub-{i}") for i in range(6)]

    for lat, lon, name in hubs:
        # Hospitals
        for k in range(2):
            w.add_hospital(Hospital(
                name=f"{name} Medical College" if k == 0 else f"{name} General Hospital",
                location=LatLon(lat=lat + rng.uniform(-0.02, 0.02),
                                lon=lon + rng.uniform(-0.02, 0.02)),
                total_beds=rng.choice([80, 120, 200, 300]),
                available_beds=rng.randint(10, 150),
                has_trauma=rng.random() > 0.3,
                has_burn_unit=rng.random() > 0.7,
            ))
        # Ambulances
        for k in range(8):
            w.add_resource(Resource(
                kind=ResourceKind.AMBULANCE,
                name=f"{name[:3].upper()}-AMB-{k+1:02d}",
                home_base=LatLon(lat=lat + rng.uniform(-0.03, 0.03),
                                 lon=lon + rng.uniform(-0.03, 0.03)),
                location=LatLon(lat=lat + rng.uniform(-0.03, 0.03),
                                lon=lon + rng.uniform(-0.03, 0.03)),
                crew_count=2,
                fuel_range_km=300,
                speed_kmh=rng.choice([75, 85, 95]),
            ))
        # Paramedic teams
        for k in range(3):
            w.add_resource(Resource(
                kind=ResourceKind.PARAMEDIC_TEAM,
                name=f"{name[:3].upper()}-PRM-{k+1:02d}",
                home_base=LatLon(lat=lat, lon=lon),
                location=LatLon(lat=lat + rng.uniform(-0.02, 0.02),
                                lon=lon + rng.uniform(-0.02, 0.02)),
                crew_count=3,
                speed_kmh=70,
            ))
        # Fire trucks
        for k in range(2):
            w.add_resource(Resource(
                kind=ResourceKind.FIRE_TRUCK,
                name=f"{name[:3].upper()}-FIR-{k+1:02d}",
                home_base=LatLon(lat=lat, lon=lon),
                location=LatLon(lat=lat + rng.uniform(-0.02, 0.02),
                                lon=lon + rng.uniform(-0.02, 0.02)),
                crew_count=4,
                speed_kmh=80,
            ))
        # Rescue teams
        for k in range(2):
            w.add_resource(Resource(
                kind=ResourceKind.RESCUE_TEAM,
                name=f"{name[:3].upper()}-RES-{k+1:02d}",
                home_base=LatLon(lat=lat, lon=lon),
                location=LatLon(lat=lat + rng.uniform(-0.02, 0.02),
                                lon=lon + rng.uniform(-0.02, 0.02)),
                crew_count=5,
                speed_kmh=60,
            ))
        # A handful of helicopters for major hubs
        if name in ("Dhaka", "Chattogram", "Barishal"):
            w.add_resource(Resource(
                kind=ResourceKind.HELICOPTER,
                name=f"{name[:3].upper()}-HLI-01",
                home_base=LatLon(lat=lat, lon=lon),
                location=LatLon(lat=lat, lon=lon),
                crew_count=3,
                speed_kmh=240,
                fuel_range_km=600,
            ))
    # Emergency operation centers (one per hub)
    for lat, lon, name in hubs:
        w.add_resource(Resource(
            kind=ResourceKind.EOC,
            name=f"{name} EOC",
            home_base=LatLon(lat=lat, lon=lon),
            location=LatLon(lat=lat, lon=lon),
            crew_count=20,
            speed_kmh=0,
            status=ResourceStatus.AVAILABLE,
        ))


# ---------- Simulator controller ----------
class Simulator:
    def __init__(self, w: World, hubs):
        self.w = w
        self.rng = random.Random(17)
        self.active = False
        self.tick = 0
        self._last_spawn = 0.0
        self.hubs = hubs

    def toggle(self, on: bool):
        self.active = on

    def step(self, dt_seconds: float = 1.0):
        if not self.active:
            return
        self.tick += 1
        self.w.advance(dt_seconds=dt_seconds)
        # Spawn new incidents at a calm, readable pace. Higher severity
        # incidents are rarer so dashboards don't flood red.
        if time.time() - self._last_spawn > 4.0 and self.rng.random() < 0.45:
            self._spawn_incident()
            self._last_spawn = time.time()
        # Rare chaos (mostly driven by the manual INJECT CHAOS buttons).
        if self.rng.random() < 0.004:
            self._random_chaos()

    def _spawn_incident(self):
        # Pick a random hub first, then spawn within ~0.7° (~70km) so the
        # response is always local and finishes during the demo window.
        hub = self.rng.choice(self.hubs)
        lat = hub[0] + self.rng.uniform(-0.6, 0.6)
        lon = hub[1] + self.rng.uniform(-0.6, 0.6)
        # Hard clamp to BD
        lat = max(21.0, min(25.8, lat))
        lon = max(88.5, min(92.3, lon))
        incident_types = ["medical", "crash", "fire", "flood", "collapse", "rescue"]
        weights = [0.45, 0.20, 0.12, 0.08, 0.08, 0.07]
        t = self.rng.choices(incident_types, weights=weights, k=1)[0]
        sev = self.rng.choices([1,2,3,4,5], weights=[0.2,0.25,0.3,0.2,0.05], k=1)[0]
        affected = max(1, int(self.rng.lognormvariate(0.5, 0.8)))
        if t in ("flood", "collapse") and self.rng.random() < 0.3:
            affected += self.rng.randint(5, 25)
        weather = "clear"
        if self.rng.random() < 0.2:
            weather = self.rng.choice(["rain", "storm", "fog"])
        road = "open"
        if self.rng.random() < 0.07:
            road = "congested"
        if self.rng.random() < 0.03:
            road = "closed"
        hazard = None
        if t in ("fire", "flood", "collapse") and self.rng.random() < 0.5:
            hazard = t if t != "collapse" else "landslide"
        notes = ""
        if t == "collapse" and self.rng.random() < 0.4:
            notes = "Multiple victims trapped under rubble."
        self.w.add_incident(Incident(
            type=t,
            location=LatLon(lat=lat, lon=lon),
            severity=Severity(sev),
            affected_count=affected,
            time_sensitivity_min=self.rng.choice([5, 8, 10, 15, 20]),
            notes=notes,
            env=EnvConditions(weather=weather, road_status=road, hazard=hazard),
            region_id="bd",
        ))
        # Immediately plan so the new incident gets a dispatch on the next
        # snapshot — otherwise it waits until the next REOPTIMIZE tick.
        self.w.plan()

    def _random_chaos(self):
        choices = ["fail_resource", "close_road", "weather_change", "hospital_full"]
        c = self.rng.choice(choices)
        if c == "fail_resource":
            movable = [r for r in self.w.resources.values()
                       if r.kind not in (ResourceKind.HOSPITAL, ResourceKind.EOC)
                       and r.status == ResourceStatus.AVAILABLE]
            if movable:
                r = self.rng.choice(movable)
                self.w.set_resource_status(r.resource_id, ResourceStatus.FAILED)
                # Auto-recover after ~30 ticks
                asyncio.get_event_loop().call_later(60, self._heal_resource, r.resource_id)
        elif c == "hospital_full":
            av = [h for h in self.w.hospitals.values() if h.available_beds > 0]
            if av:
                h = self.rng.choice(av)
                self.w.set_hospital_capacity(h.hospital_id, 0)
                asyncio.get_event_loop().call_later(45, self._restore_hospital, h.hospital_id, h.total_beds // 4)
        elif c == "weather_change":
            for inc in list(self.w.incidents.values()):
                if inc.status in ("REPORTED", "TRIAGED", "RESPONDING"):
                    inc.env.weather = self.rng.choice(["rain", "storm", "clear"])
                    break

    def _heal_resource(self, rid):
        if rid in self.w.resources:
            self.w.set_resource_status(rid, ResourceStatus.AVAILABLE)

    def _restore_hospital(self, hid, beds):
        if hid in self.w.hospitals:
            self.w.set_hospital_capacity(hid, beds)


simulator = Simulator(world, HUBS)


# ---------- FastAPI app & background loops ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_default_world(world, SCENARIO)
    simulator.toggle(SIM_DEFAULT)

    async def tick_loop():
        last_plan = 0.0
        last_broadcast = 0.0
        while True:
            try:
                simulator.step(dt_seconds=TICK_MS / 1000.0 * SIM_SPEED)
                now = time.time()
                # Count active incidents that still need a dispatch (REPORTED
                # with no active dispatches yet). If any exist, plan sooner so
                # they're not left waiting on the REOPTIMIZE cadence.
                dispatched_incs = {d.incident_id for d in world.dispatches.values()
                                   if d.state not in ("COMPLETED", "REJECTED")}
                pending = [i for i in world.incidents.values()
                           if i.status in ("REPORTED",) and i.incident_id not in dispatched_incs]
                # Also pick up incidents whose active dispatch recently
                # completed (on-scene→transporting needs a hospital leg, etc.)
                # and any active incident whose assigned resource is FAILED.
                if not pending:
                    for i in world.incidents.values():
                        if i.status in ("RESOLVED", "CANCELLED"):
                            continue
                        has_live = False
                        for d in world.dispatches.values():
                            if d.incident_id == i.incident_id and d.state not in ("COMPLETED","REJECTED"):
                                has_live = True
                                break
                        if not has_live:
                            pending.append(i)
                active_count = sum(1 for i in world.incidents.values()
                                   if i.status not in ("RESOLVED", "CANCELLED"))
                need_plan = bool(pending) or (simulator.active and active_count > 0 and now - last_plan > REOPTIMIZE_MS / 1000.0)
                if need_plan:
                    world.plan()
                    last_plan = now
                if now - last_broadcast > 0.5:
                    await broadcast_snapshot()
                    last_broadcast = now
            except Exception as e:
                print(f"[tick] error: {e}")
            await asyncio.sleep(TICK_MS / 1000.0)

    task = asyncio.create_task(tick_loop())
    yield
    task.cancel()


app = FastAPI(title="AEGIS-ER API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the dashboard statically if present (Docker build and local dev via
# scripts/run_dev.sh). The dashboard path is resolved relative to this file
# or to an explicit AEGIS_DASHBOARD_DIR env var.
import pathlib
_DASH_DIR = pathlib.Path(os.environ.get("AEGIS_DASHBOARD_DIR",
    str(pathlib.Path(__file__).resolve().parent.parent / "dashboard")))
if _DASH_DIR.is_dir():
    # Mount static assets on a named path so they don't shadow /api or /ws.
    app.mount("/ui", StaticFiles(directory=str(_DASH_DIR), html=True), name="ui")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root_redirect():
        return FileResponse(str(_DASH_DIR / "index.html"))

    # Also serve top-level asset paths (style.css, app.js, config.js, etc.)
    _ASSET_FILES = {"index.html", "app.js", "style.css", "config.js"}
    @app.get("/{asset}", include_in_schema=False)
    def asset(asset: str):
        if asset in _ASSET_FILES:
            p = _DASH_DIR / asset
            if p.is_file():
                return FileResponse(str(p))
        raise HTTPException(404)


# ---------- WebSocket hub ----------
class WSHub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)

    async def unregister(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        data = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)


hub = WSHub()


async def broadcast_snapshot():
    await hub.broadcast({"type": "snapshot", "data": world.snapshot(), "ts": datetime.now(timezone.utc).isoformat()})


# ---------- REST endpoints ----------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "tick": world.tick,
        "simulator": simulator.active,
        "incidents_active": sum(1 for i in world.incidents.values() if i.status not in ("RESOLVED", "CANCELLED")),
        "resources_total": len(world.resources),
        "dispatches_total": len(world.dispatches),
        "solver_budget_ms": SOLVER_BUDGET_MS,
    }


@app.get("/api/state")
def get_state():
    return world.snapshot()


@app.post("/api/incidents")
def create_incident(body: IncidentIn):
    inc = Incident(
        type=body.type,
        location=_latlon(body.location),
        severity=Severity(body.severity),
        affected_count=body.affected_count,
        time_sensitivity_min=body.time_sensitivity_min,
        notes=body.notes,
        region_id=body.region_id,
        env=EnvConditions(weather=body.weather, road_status=body.road_status, hazard=body.hazard),
    )
    world.add_incident(inc)
    world.plan()
    return inc


@app.post("/api/resources")
def create_resource(body: ResourceIn):
    loc = _latlon(body.location) if body.location else _latlon(body.home_base)
    res = Resource(
        kind=ResourceKind(body.kind),
        name=body.name or f"{body.kind.upper()}-{len(world.resources)+1:04d}",
        home_base=_latlon(body.home_base),
        location=loc,
        crew_count=body.crew_count,
        speed_kmh=body.speed_kmh,
    )
    world.add_resource(res)
    return res


@app.post("/api/hospitals")
def create_hospital(body: HospitalIn):
    h = Hospital(
        name=body.name,
        location=_latlon(body.location),
        total_beds=body.total_beds,
        available_beds=body.available_beds,
        has_trauma=body.has_trauma,
        has_burn_unit=body.has_burn_unit,
    )
    world.add_hospital(h)
    return h


@app.post("/api/env-event")
def inject_env_event(body: EnvEventIn):
    """Inject a disruption for chaos/what-if testing."""
    if body.kind == "resource_fail" and body.target_id:
        if body.target_id not in world.resources:
            raise HTTPException(404, "resource not found")
        world.set_resource_status(body.target_id, ResourceStatus.FAILED)
        return {"ok": True}
    if body.kind == "resource_online" and body.target_id:
        if body.target_id not in world.resources:
            raise HTTPException(404, "resource not found")
        world.set_resource_status(body.target_id, ResourceStatus.AVAILABLE)
        return {"ok": True}
    if body.kind == "hospital_full" and body.target_id:
        if body.target_id not in world.hospitals:
            raise HTTPException(404, "hospital not found")
        world.set_hospital_capacity(body.target_id, 0)
        return {"ok": True}
    if body.kind == "weather_change":
        for inc in world.incidents.values():
            if inc.status not in ("RESOLVED", "CANCELLED"):
                inc.env.weather = body.value or "storm"
        return {"ok": True}
    if body.kind == "close_road":
        # Simulate road closure by setting congestion on all active incidents in region
        for inc in world.incidents.values():
            if inc.status not in ("RESOLVED", "CANCELLED"):
                inc.env.road_status = "closed"
        return {"ok": True}
    raise HTTPException(400, f"unknown env event {body.kind}")


@app.post("/api/actions")
async def perform_action(body: ActionIn):
    if body.action == "plan":
        ds = world.plan()
        await broadcast_snapshot()
        return {"ok": True, "new_dispatches": len(ds)}
    if body.action == "tick":
        # Manual ADVANCE — bigger jumps so users can skip ahead. Always run
        # a plan cycle after advancing so any new REPORTED incidents get
        # dispatches and progress starts.
        for _ in range(body.steps or 1):
            simulator.step(dt_seconds=60.0)
        world.plan()
        await broadcast_snapshot()
        return {"ok": True}
    if body.action == "sim_start":
        simulator.toggle(True)
        # Seed one incident immediately on START so the demo never shows a
        # blank map waiting for the 4s spawn timer — first dispatch appears
        # the moment the operator hits the button.
        if not any(i.status not in ("RESOLVED","CANCELLED") for i in world.incidents.values()):
            simulator._spawn_incident()
            world.plan()
            await broadcast_snapshot()
        return {"ok": True, "simulator": True}
    if body.action == "sim_stop":
        simulator.toggle(False)
        return {"ok": True, "simulator": False}
    if body.action == "reset":
        # Reset world: stop simulator, clear state, re-bootstrap
        simulator.toggle(False)
        world.incidents.clear()
        world.resources.clear()
        world.hospitals.clear()
        world.dispatches.clear()
        world._dispatch_progress.clear()
        world.tick = 0
        _bootstrap_default_world(world, SCENARIO)
        await broadcast_snapshot()
        return {"ok": True, "simulator": False}
    raise HTTPException(400, f"unknown action {body.action}")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.register(ws)
    try:
        # Send immediate snapshot
        await ws.send_text(json.dumps({"type": "snapshot", "data": world.snapshot()}, default=str))
        while True:
            # We don't expect inbound messages beyond ping; keep alive
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if msg == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(ws)


@app.get("/api/kpi")
def kpi():
    s = world.snapshot()
    resolved = [i for i in world.incidents.values() if i.status == "RESOLVED"]
    total = list(world.incidents.values())
    return {
        "total_incidents": len(total),
        "resolved": len(resolved),
        "active": s["active_count"],
        "utilization": s["utilization"],
        "mean_eta_seconds": s["mean_eta_seconds"],
        "resources": len(world.resources),
        "hospitals": len(world.hospitals),
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
