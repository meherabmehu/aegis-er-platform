"""Human- and machine-readable rationale generation for dispatches."""
from __future__ import annotations

from .types import Dispatch, Incident, Resource, ResourceStatus


def explain_dispatch(d: Dispatch, inc: Incident, chosen: Resource,
                     considered: list[tuple[Resource, float, str]]) -> dict:
    """Build an explanation object.

    considered: list of (resource, cost, reason_rejected_if_not_chosen)
    """
    rejected = []
    for r, c, reason in considered:
        if r.resource_id == chosen.resource_id:
            continue
        rejected.append({
            "resource_id": r.resource_id,
            "name": r.name or r.kind.value,
            "kind": r.kind.value,
            "cost": round(c, 2),
            "reason": reason,
            "distance_m": round(_dist(r, inc), 1),
        })
    text = (
        f"{chosen.name or chosen.kind.value} dispatched to {inc.type} incident "
        f"({inc.severity.name}, {inc.affected_count} affected). "
        f"ETAs {d.eta_seconds/60:.1f} min; urgency {inc.urgency_score:.2f}."
    )
    return {
        "summary": text,
        "chosen": {
            "resource_id": chosen.resource_id,
            "name": chosen.name or chosen.kind.value,
            "kind": chosen.kind.value,
            "distance_m": round(_dist(chosen, inc), 1),
            "eta_seconds": d.eta_seconds,
        },
        "rejected": rejected[:5],
        "urgency": inc.urgency_score,
        "optimality_gap": d.optimality_gap,
    }


def _dist(r: Resource, inc: Incident) -> float:
    from .geo import haversine_m
    return haversine_m(r.location, inc.location)
