# Architecture

AEGIS-ER is a tick-driven, event-sourced dispatch platform split cleanly between a pure-Python core engine (`libs/aegis`) and a FastAPI service that exposes it over REST + WebSocket and serves the dashboard.

```
                ┌─────────────────────────────────────────┐
                │   INGRESS  (Citizen / Sim / Telemetry)  │
                └─────────────┬───────────────────────────┘
                              ▼
                ┌─────────────────────────────────────────┐
                │   FastAPI Gateway  (REST + WebSocket)   │
                └─────────────┬───────────────────────────┘
                              ▼
 ┌──────────────┐  ┌────────────────────┐  ┌──────────────┐  ┌────────────────┐
 │ World State  │→ │  Hybrid Solver     │→ │ Explain/PRI  │→ │ Dispatch Queue │
 │ (Hubs/Units  │  │  Greedy→Hungarian  │  │ Confidence   │  │ State Machine  │
 │  Incidents)  │  │  →CP-SAT→LNS(200ms)│  │ Rationale    │  │ Re-route/Div   │
 └──────┬───────┘  └─────────┬──────────┘  └──────────────┘  └───────┬────────┘
        │                   │                                     │
        └───────────────────▼─────────────────────────────────────┘
                              ▼
                ┌─────────────────────────────────────────┐
                │ WS Push (4 Hz) → Dashboard / EOC / App │
                └─────────────────────────────────────────┘
```

A high-resolution rendered diagram is at [`docs/img/architecture.svg`](img/architecture.svg).

## Core Packages

| Package | Responsibility |
|---------|---------------|
| `aegis.types` | Domain enums and dataclasses: `Severity`, `ResourceKind`, `ResourceStatus`, `DispatchState`, `Incident`, `Resource`, `Dispatch`, `EnvConditions`. |
| `aegis.geo` | Haversine distance, bearing, point offset, nearest-k lookup. |
| `aegis.routing` | `HaversineRouter`: builds jittered 4–8-point polylines, with `estimate_route_for_resource` (helicopters fly straight-line, ground units follow a curved path scaled by straightness factor). |
| `aegis.priority` | `PriorityScorer`: converts severity, affected count, time sensitivity, weather, and road status into a scalar urgency score. |
| `aegis.explain` | Produces a plain-English rationale list, rejected-reason tags, and an optimality-gap-based confidence score for every dispatch. |
| `aegis.solver` | 4-phase hybrid: (1) greedy warm-start, (2) Hungarian optimal bipartite on the cost matrix, (3) CP-SAT integer program for side constraints (crew capacity, medical→hospital, kind-matching), (4) Large Neighborhood Search polish. Always returns a feasible plan within the configured budget (default 200 ms). |
| `aegis.world` | `World` owns all mutable state and drives the simulation tick. `plan()` invokes the solver on the uncovered incident set. `advance()` lerps every en-route/transporting dispatch from a plan-time-captured `from_loc` to its target (so routes stay stable even when the resource's live location drifts mid-tick), runs the on-scene timer (`8 + 3·N` sim-seconds), performs hospital selection, and calls `_complete_dispatch` to mark incidents `RESOLVED`. Stale progress entries for completed/rejected dispatches or resolved/cancelled incidents are purged at the top of every tick — this was the historical root cause of the "resolved counter bounces" bug. |

## Service Layer (`services/assignment-solver/app.py`)

FastAPI service that:

- Serves static dashboard assets (`index.html`, `app.js`, `style.css`, `config.js`).
- Exposes REST endpoints for health, state, incidents, environment events (chaos), and actions (`plan`, `tick`, `sim_start`, `sim_stop`, `reset`).
- Maintains a `Simulator` that owns the `World` and a Bangladesh preset (8 divisional hubs, 16 hospitals, 123 response units).
- Runs a tick loop: every `TICK_MS` wall-clock milliseconds it advances the world by `SIM_SPEED` sim-seconds, and every `REOPTIMIZE_MS` it replans pending dispatches.
- Broadcasts a full world snapshot over WebSocket every tick (~4 Hz).
- Seeds one incident on `sim_start` so the dashboard never boots on an empty map.
- Garbage-collects completed dispatches every 200 ticks to keep the snapshot small.

## Dashboard (`services/dashboard`)

Vanilla JS + Leaflet dark mission-control dashboard. Major subsystems:

- **Map & layers** — `L.map` tightly clamped to Bangladesh bounds (`BD_BOUNDS`), with OSM tiles processed through a mission-control CSS filter. Incidents use severity-colored divIcons (S5 critical pulsing pin), resources use kind-colored dots, hospitals use a blue H (red pulsing when full). Smart grid-clustering for non-critical incidents at zoom ≤ 9 ensures S5 pins ALWAYS render individually.
- **KPI strip** — 7 cards (Critical, Active, Available, Deployed, Avg ETA, Utilization, Resolved) with sparklines, trend arrows (up/down, colored by whether higher is bad), and a resolved-pulse green burst on every completion.
- **Incident queue** — Top 5 ranked by urgency score, with severity bars, affected count, urgency %, nearest-unit ETA, and a "▶ START to move" hint when sim is paused.
- **AI Decision Panel** — Selected incident drives the XAI card: confidence ring (color-coded green/cyan/yellow/red), 6 input factors (severity, affected, weather, roads, crew, speed), chosen unit, ETA, distance, target hospital, and up to 6 rationale bullets (including conditional FAILOVER/DIVERT/ROAD/WEATHER reasons).
- **Chaos panel** — Four injectors: close road (orange detour + bent polyline), storm (purple banner + speed penalty), hospital full (red diverts), unit fail (yellow failover). All injectors fire `flashReroute` (full-map banner, screen vignette, polyline glow, detour badges) and refocus the map: road/storm first zoom out to BD-wide then zoom back into the top active incident so the reroute is system-visible.
- **Manual incident form** — Click anywhere on the map, fills lat/lon (clamped to BD response area), submit posts the incident, runs one plan cycle, shows "✅ Dispatch locked in" toast, and nudges the user to press Start. Does NOT auto-start the simulator (operator stays in control).
- **Guided Demo Mode** — 14-step ~55-second walkthrough that resets the world, forces all layers on, starts the sim, and programmatically flies/highlights: overview → S5 spawn → XAI inputs → XAI rationale → dispatch → arrival → layers + filters → 4 chaos events → transport → resolve → summary.
- **Notification calm** — Toasts capped at 4 concurrent, severity-throttled (S5 = toast + feed, S4 = toast + feed, S1–S3 = feed only), batched per render pass, deduped across reconnects via `_seenDispatchIds` and `_seenIncidents`.
- **Cold start** — On page load the dashboard issues `reset` + `sim_stop`, fetches `/api/state` to seed its `_seen*` sets, then opens the WebSocket; a 2-second polling fallback keeps UI alive if WS drops.

## Resilience Guarantees

1. **No double-assignment** — Stale progress entries are purged at the top of every `advance()`; `_complete_dispatch` flips incident status to `RESOLVED` and only a fresh plan can re-dispatch.
2. **Units always arrive** — Every dispatch captures `from_loc` at plan time and on the on_scene→transporting transition, and lerps from that captured point rather than the drifting live `r.location`; this prevents re-targeting and the "unit spins forever" bug.
3. **Chaos visibility** — Every chaos event triggers a banner, vignette, polyline flash, detour bend, and colored midpoint badge (dual badges for ROAD+STORM combos).
4. **Hospital awareness** — `is_medical` dispatches (ambulance + medical/crash/collapse) always route to the nearest hospital with available beds; `hospital_full` events immediately divert in-flight transports to the next-closest open facility.
5. **Failover** — Resource failures mark the unit `FAILED`; a replan assigns the next-best candidate and injects a `FAILOVER` rationale bullet. The failed unit returns to service after 45 s.
6. **Operated-controlled pacing** — Manual reports plan once but never auto-start the sim; operator presses START to advance. Demo Mode auto-advances on a fixed timeline.

## Deployment

Pure Python. No Docker is required:

```bash
pip install -e libs/aegis
pip install fastapi uvicorn pydantic httpx numpy ortools
PYTHONPATH=libs/aegis AEGIS_DASHBOARD_DIR=services/dashboard python services/assignment-solver/app.py
```

Launch helpers are provided for Windows (`start.bat`, `run.ps1`) and POSIX (`run.sh`, `Makefile`). The dashboard is served by FastAPI's static file mount at `/`, so a single port (default 8000) exposes both API and UI.
