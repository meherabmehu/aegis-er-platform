"""Domain types shared across all AEGIS-ER services.

Uses pydantic for strict validation at service boundaries. Keep this file free
of heavy dependencies so it can be imported anywhere (hot paths, tests, sims).
"""
from __future__ import annotations

import uuid
import enum
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, ConfigDict


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LatLon(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

    def as_tuple(self) -> tuple[float, float]:
        return self.lat, self.lon


class Severity(int, enum.Enum):
    MINOR = 1
    MODERATE = 2
    MAJOR = 3
    SEVERE = 4
    CRITICAL = 5


class ResourceKind(str, enum.Enum):
    AMBULANCE = "ambulance"
    PARAMEDIC_TEAM = "paramedic_team"
    RESCUE_TEAM = "rescue_team"
    FIRE_TRUCK = "fire_truck"
    HELICOPTER = "helicopter"
    HOSPITAL = "hospital"
    EOC = "eoc"


class ResourceStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    DISPATCHED = "DISPATCHED"
    ON_SCENE = "ON_SCENE"
    TRANSPORTING = "TRANSPORTING"
    MAINTENANCE = "MAINTENANCE"
    FAILED = "FAILED"


class DispatchState(str, enum.Enum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    EN_ROUTE = "EN_ROUTE"
    ON_SCENE = "ON_SCENE"
    TRANSPORTING = "TRANSPORTING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    REROUTED = "REROUTED"


class EnvConditions(BaseModel):
    weather: str = "clear"         # clear | rain | storm | cyclone | fog | snow
    wind_kmh: float = 0.0
    visibility_m: float = 5000.0
    road_status: str = "open"      # open | congested | closed
    hazard: Optional[str] = None   # flood | fire | chemical | landslide | None

    def traffic_multiplier(self) -> float:
        """Speed multiplier applied to travel (less than 1.0 = slower)."""
        m = 1.0
        if self.road_status == "congested":
            m *= 0.6
        elif self.road_status == "closed":
            m *= 0.05  # barely passable
        if self.weather in ("rain",):
            m *= 0.85
        if self.weather in ("storm", "cyclone"):
            m *= 0.55
        if self.visibility_m < 200:
            m *= 0.75
        return m


class Incident(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    incident_id: str = Field(default_factory=_uuid)
    region_id: str = "default"
    type: str = "medical"          # medical | fire | flood | crash | collapse | hazmat | rescue
    location: LatLon
    severity: Severity = Severity.MAJOR
    affected_count: int = 1
    time_sensitivity_min: int = 10  # golden window
    env: EnvConditions = Field(default_factory=EnvConditions)
    resource_needs: dict[str, int] = Field(default_factory=dict)
    notes: str = ""
    reported_at: datetime = Field(default_factory=_utcnow)
    resolved_at: Optional[datetime] = None
    status: str = "REPORTED"       # REPORTED | TRIAGED | RESPONDING | TRANSPORT | RESOLVED | CANCELLED
    urgency_score: float = 0.0

    def required_of(self, kind: ResourceKind) -> int:
        return int(self.resource_needs.get(kind.value, 0) or self._default_needs().get(kind.value, 0))

    def _default_needs(self) -> dict[str, int]:
        # Sensible defaults if caller doesn't specify
        sev = self.severity.value
        base = {ResourceKind.AMBULANCE.value: 1}
        if self.type in ("fire", "collapse"):
            base[ResourceKind.FIRE_TRUCK.value] = 1
            base[ResourceKind.RESCUE_TEAM.value] = 1
        if self.type in ("flood", "rescue"):
            base[ResourceKind.RESCUE_TEAM.value] = 1
            # Helicopter only for S5 critical flood/rescue; otherwise ground
            # rescue can reach — keeps S4 demos winnable with our 3 helos.
            if sev >= 5:
                base[ResourceKind.HELICOPTER.value] = 1
        if sev >= 4:
            base[ResourceKind.PARAMEDIC_TEAM.value] = 1
        if self.type == "hazmat":
            base[ResourceKind.RESCUE_TEAM.value] = 1
        if self.affected_count >= 10:
            base[ResourceKind.AMBULANCE.value] = min(4, 1 + self.affected_count // 5)
            base[ResourceKind.EOC.value] = 1
        return base


class Resource(BaseModel):
    resource_id: str = Field(default_factory=_uuid)
    kind: ResourceKind
    name: str = ""
    home_base: LatLon
    location: LatLon
    location_updated: datetime = Field(default_factory=_utcnow)
    capacity: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    status: ResourceStatus = ResourceStatus.AVAILABLE
    current_incident_id: Optional[str] = None
    dispatch_id: Optional[str] = None
    version: int = 0
    crew_count: int = 2
    fuel_range_km: float = 250.0
    speed_kmh: float = 0.0   # 0 -> use default per kind

    def effective_speed_kmh(self) -> float:
        if self.speed_kmh > 0:
            return self.speed_kmh
        defaults = {
            ResourceKind.AMBULANCE: 85.0,
            ResourceKind.PARAMEDIC_TEAM: 70.0,
            ResourceKind.FIRE_TRUCK: 80.0,
            ResourceKind.RESCUE_TEAM: 60.0,
            ResourceKind.HELICOPTER: 240.0,
            ResourceKind.HOSPITAL: 0.0,
            ResourceKind.EOC: 0.0,
        }
        return defaults.get(self.kind, 50.0)


class Hospital(BaseModel):
    hospital_id: str = Field(default_factory=_uuid)
    name: str
    location: LatLon
    total_beds: int = 50
    available_beds: int = 50
    has_trauma: bool = True
    has_burn_unit: bool = False

    @property
    def capacity_ratio(self) -> float:
        return self.available_beds / max(1, self.total_beds)


class Dispatch(BaseModel):
    dispatch_id: str = Field(default_factory=_uuid)
    incident_id: str
    resource_id: str
    eta_seconds: float = 0.0
    distance_m: float = 0.0
    route: list[LatLon] = Field(default_factory=list)
    rationale: dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime = Field(default_factory=_utcnow)
    state: DispatchState = DispatchState.PROPOSED
    target_hospital_id: Optional[str] = None
    cost: float = 0.0
    optimality_gap: float = 0.0
