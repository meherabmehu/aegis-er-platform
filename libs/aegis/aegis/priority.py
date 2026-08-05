"""Priority (urgency) scoring.

Combines rule-based hard overrides with a weighted feature model. In a full
deployment the weights would come from a calibrated gradient-boosted model; we
keep them here as clear constants so the system is fully explainable and easy
to tune without retraining.
"""
from __future__ import annotations

from dataclasses import dataclass

from .types import Incident, Severity


# Weighting calibrated so that:
#   - a CRITICAL, 20-affected, flood-entrapped, 5-minute-window incident → ≈ 1.0
#   - a MINOR, 1-affected, 30-minute-window clear-day incident → ≈ 0.15
W_SEVERITY = 0.40
W_AFFECTED = 0.20
W_TIME = 0.20
W_ENV = 0.12
W_SCARCE = 0.08


@dataclass
class ScoreBreakdown:
    severity: float
    affected: float
    time_pressure: float
    environment: float
    scarcity: float
    override: float | None
    final: float
    reasons: list[str]


class PriorityScorer:
    def __init__(self, regional_resource_density: float = 1.0):
        # regional_resource_density: resources within response radius, normalized
        # to 1.0 = "good coverage"; <1 multiplies scarcity term.
        self.density = max(0.05, min(2.0, regional_resource_density))

    def score(self, incident: Incident, seconds_elapsed: float = 0.0, local_available: int = 10) -> ScoreBreakdown:
        sev = (incident.severity.value - 1) / 4.0  # 0..1
        aff = min(1.0, incident.affected_count / 20.0)
        remaining_s = max(1.0, incident.time_sensitivity_min * 60.0 - seconds_elapsed)
        time_p = min(1.0, (incident.time_sensitivity_min * 60.0) / (remaining_s * 3.0))  # grows as deadline nears
        env = self._env_risk(incident)
        scarcity = min(1.0, 3.0 / max(1, local_available) / self.density)

        base = (
            W_SEVERITY * sev
            + W_AFFECTED * aff
            + W_TIME * time_p
            + W_ENV * env
            + W_SCARCE * scarcity
        )
        reasons = []
        override = None
        if incident.severity == Severity.CRITICAL and incident.affected_count >= 10:
            override = 0.98
            reasons.append("mass-casualty incident (≥10 affected + critical)")
        if incident.affected_count >= 20:
            override = max(override or 0.0, 0.96)
            reasons.append("large-scale casualty event (≥20 affected)")
        if "entrap" in incident.notes.lower() or "trapped" in incident.notes.lower():
            override = max(override or 0.0, 0.9)
            reasons.append("entrapped victims reported")
        if incident.env.hazard in ("flood", "fire", "chemical", "landslide") and incident.severity.value >= 4:
            val = 0.85
            override = max(override or 0.0, val)
            reasons.append(f"active environmental hazard: {incident.env.hazard}")

        final = min(1.0, max(0.05, override if override is not None else base))
        return ScoreBreakdown(
            severity=round(sev, 3),
            affected=round(aff, 3),
            time_pressure=round(time_p, 3),
            environment=round(env, 3),
            scarcity=round(scarcity, 3),
            override=override,
            final=round(final, 3),
            reasons=reasons,
        )

    @staticmethod
    def _env_risk(incident: Incident) -> float:
        risk = 0.0
        if incident.env.weather in ("storm", "cyclone"):
            risk += 0.5
        elif incident.env.weather == "rain":
            risk += 0.2
        if incident.env.visibility_m < 500:
            risk += 0.2
        if incident.env.road_status == "closed":
            risk += 0.4
        elif incident.env.road_status == "congested":
            risk += 0.15
        if incident.env.hazard:
            risk += {"flood": 0.4, "fire": 0.35, "chemical": 0.5, "landslide": 0.45}.get(incident.env.hazard, 0.2)
        return min(1.0, risk)
