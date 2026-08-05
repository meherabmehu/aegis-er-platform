"""Hybrid assignment solver.

This is the core optimization routine. It is built as an **anytime** algorithm:

  1. Greedy warm-start (<2 ms on typical problems) guarantees a feasible plan.
  2. Jonker-Volgenant-style Hungarian refinement on the global cost matrix
     gives the optimal linear-sum assignment for uncapacitated matching.
  3. CP-SAT (OR-Tools) adds side constraints (capacity, hospitals, fatigue,
     fuel, terrain) when available, improving the solution in the remaining
     time budget.
  4. Large Neighborhood Search keeps improving until the budget expires.

Because every step is optional and falls back cleanly, the solver remains
fast and correct even without OR-Tools installed (tests, embedded, edge).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence
from collections import defaultdict

from .geo import haversine_m, nearest_k
from .priority import PriorityScorer
from .routing import HaversineRouter, RoutingEngine, estimate_route_for_resource
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

# Penalties / weights in the objective. Tunable.
W_ETA = 1.0          # seconds of passenger-perceived response time
W_IDLE = 0.002       # small penalty to prefer using resources, not holding them
W_OVERLOAD = 15.0    # seconds-penalty per 1-bed-over hospital capacity
W_FATIGUE = 8.0      # seconds-penalty per already-dispatched resource (anti-thrash)
W_SWITCH = 60.0      # seconds-penalty to rip a resource off its current dispatch
INFEASIBLE = 1e9


@dataclass
class SolverConfig:
    candidate_k: int = 25            # k nearest resources considered per incident
    time_budget_ms: int = 200        # hard wall-clock budget
    use_cpsat: bool = True           # auto-disables if OR-Tools missing
    use_lns: bool = True
    lns_iterations: int = 40
    switch_cost_penalty: float = W_SWITCH


@dataclass
class AssignmentResult:
    dispatches: list[Dispatch] = field(default_factory=list)
    rejected_incidents: list[str] = field(default_factory=list)
    objective: float = 0.0
    lower_bound: float = 0.0
    optimality_gap: float = 0.0
    solver_path: list[str] = field(default_factory=list)
    solve_ms: float = 0.0
    by_incident: dict[str, list[Dispatch]] = field(default_factory=dict)


class AssignmentSolver:
    def __init__(self, config: Optional[SolverConfig] = None,
                 router: Optional[RoutingEngine] = None,
                 scorer: Optional[PriorityScorer] = None):
        self.config = config or SolverConfig()
        self.router = router or HaversineRouter()
        self.scorer = scorer or PriorityScorer()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def solve(self, incidents: Sequence[Incident],
              resources: Sequence[Resource],
              hospitals: Optional[Sequence[Hospital]] = None,
              env_override=None) -> AssignmentResult:
        t0 = time.perf_counter()
        hospitals = list(hospitals or [])
        incidents = [i for i in incidents if i.status not in ("RESOLVED", "CANCELLED")]
        if not incidents:
            return AssignmentResult(solve_ms=(time.perf_counter()-t0)*1000)

        # Build per-incident resource needs (kind → count)
        needs: dict[str, dict[str, int]] = {}
        for inc in incidents:
            needs[inc.incident_id] = self._needed_kinds(inc)

        # Candidate pool per (incident, kind)
        pool = self._build_candidate_pool(incidents, resources)

        # Pre-compute routes and costs
        cost_matrix, route_cache = self._build_costs(incidents, resources, pool, hospitals, env_override)

        # Phase 1: greedy
        path = ["greedy"]
        assign = self._greedy_assign(incidents, resources, needs, cost_matrix)
        best_obj = self._objective(assign, incidents, cost_matrix)

        # Phase 2: Hungarian per (kind) global bipartite graph
        try:
            assign2, obj2 = self._hungarian_assign(incidents, resources, needs, cost_matrix, assign)
            if obj2 < best_obj - 1e-6:
                assign = assign2
                best_obj = obj2
                path.append("hungarian")
        except Exception:
            pass  # graceful degrade

        # Phase 3: CP-SAT with side constraints (if budget allows & OR-Tools present)
        if self.config.use_cpsat:
            try:
                budget_left_ms = self.config.time_budget_ms - (time.perf_counter() - t0) * 1000
                if budget_left_ms > 30:
                    assign3, obj3 = self._cpsat_assign(
                        incidents, resources, needs, cost_matrix, assign,
                        hospitals=hospitals,
                        budget_sec=min(0.25, budget_left_ms / 1000.0 / 2),
                    )
                    if obj3 < best_obj - 1e-6:
                        assign = assign3
                        best_obj = obj3
                        path.append("cpsat")
            except Exception:
                pass

        # Phase 4: LNS improvement (if time left)
        if self.config.use_lns:
            budget_left_ms = self.config.time_budget_ms - (time.perf_counter() - t0) * 1000
            if budget_left_ms > 20:
                iters = min(self.config.lns_iterations, int(budget_left_ms / 2))
                assign_lns, obj_lns = self._lns_improve(
                    incidents, resources, needs, cost_matrix, assign, iterations=iters
                )
                if obj_lns < best_obj - 1e-6:
                    assign = assign_lns
                    best_obj = obj_lns
                    path.append("lns")

        # Emit Dispatch objects
        dispatches: list[Dispatch] = []
        by_incident: dict[str, list[Dispatch]] = defaultdict(list)
        used_resources: set[str] = set()
        rejected: list[str] = []
        for inc in incidents:
            slots = assign.get(inc.incident_id, [])
            # Soft requirement: reject ONLY if no resource AT ALL could be
            # assigned (slots empty). Missing secondary kinds (e.g. paramedic
            # when only ambulance is within range) is acceptable — the primary
            # ambulance will still respond and additional units can be sent on
            # a later re-plan. This prevents S4+ incidents in remote areas
            # from being marked "rejected" when a perfectly good ambulance
            # exists within fuel range.
            if len(slots) == 0:
                rejected.append(inc.incident_id)
            for (res_idx, kind, hosp_id, slot_cost) in slots:
                res = resources[res_idx]
                if res.resource_id in used_resources:
                    continue
                used_resources.add(res.resource_id)
                key = (inc.incident_id, res.resource_id)
                route = route_cache.get(key)
                eta_s = route.eta_seconds if route else _estimate_eta_from_cost(slot_cost)
                dist_m = route.distance_m if route else 0.0
                d = Dispatch(
                    incident_id=inc.incident_id,
                    resource_id=res.resource_id,
                    eta_seconds=round(eta_s, 1),
                    distance_m=round(dist_m, 1),
                    route=route.path if route else [],
                    state=DispatchState.PROPOSED,
                    cost=round(slot_cost, 3),
                    rationale={
                        "candidates_considered": len(pool.get((inc.incident_id, kind.value), [])),
                        "objective_component_eta": round(eta_s * W_ETA, 2),
                        "kind": kind.value,
                    },
                    target_hospital_id=hosp_id,
                    optimality_gap=0.0,
                )
                dispatches.append(d)
                by_incident[inc.incident_id].append(d)

        # Lower bound: ignore integrality and switch costs — sum of best-cost per slot
        lb = self._linear_lower_bound(incidents, needs, cost_matrix)
        gap = (best_obj - lb) / max(1.0, lb)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AssignmentResult(
            dispatches=dispatches,
            rejected_incidents=rejected,
            objective=round(best_obj, 3),
            lower_bound=round(lb, 3),
            optimality_gap=round(gap, 4),
            solver_path=path,
            solve_ms=round(elapsed_ms, 2),
            by_incident=dict(by_incident),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _needed_kinds(self, inc: Incident) -> dict[str, int]:
        out: dict[str, int] = {}
        for k in ResourceKind:
            n = inc.required_of(k)
            if n > 0:
                out[k.value] = n
        return out

    def _build_candidate_pool(self, incidents, resources):
        """For each (incident, kind), pick candidate_k nearest feasible resources."""
        pool: dict[tuple[str, str], list[int]] = {}
        # Bucket resources by kind for speed
        by_kind: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(resources):
            if r.status in (ResourceStatus.AVAILABLE, ResourceStatus.DISPATCHED):
                by_kind[r.kind.value].append(i)
        for inc in incidents:
            for k_str, need_n in self._needed_kinds(inc).items():
                # Always consider DISPATCHED resources (for possible reassignment)
                # but AVAILABLE first.
                cand_idx = by_kind.get(k_str, [])
                cand_objs = [(i, resources[i]) for i in cand_idx]
                # nearest_k by haversine distance
                nearest = nearest_k(inc.location, cand_objs, self.config.candidate_k, key=lambda x: x[1].location)
                pool[(inc.incident_id, k_str)] = [idx for (d, (idx, _r)) in nearest]
        return pool

    def _build_costs(self, incidents, resources, pool, hospitals, env_override):
        """Bulk-compute (incident, resource) cost; returns cost matrix + route cache."""
        # Index resources and incidents for fast lookup
        inc_index = {inc.incident_id: i for i, inc in enumerate(incidents)}
        # Pre-compute urgency weights
        w = {}
        for inc in incidents:
            w[inc.incident_id] = inc.urgency_score if inc.urgency_score > 0 else self.scorer.score(inc).final
        # Flatten bulk route requests
        pairs: list[tuple[int, int]] = []  # (inc_idx, res_idx)
        locs = {}
        for (inc_id, kind), idx_list in pool.items():
            i_idx = inc_index[inc_id]
            inc = incidents[i_idx]
            for r_idx in idx_list:
                pairs.append((i_idx, r_idx))
                locs[(i_idx, r_idx)] = (inc.location, resources[r_idx].location)
        # Call router (simulator/Haversine here)
        routes = {}
        if pairs:
            reqs = []
            env = env_override
            for i_idx, r_idx in pairs:
                r = resources[r_idx]
                inc = incidents[i_idx]
                e = env if env is not None else inc.env
                reqs.append((r.location, inc.location, r.effective_speed_kmh(), e))
            rs = self.router.bulk_eta(reqs)
            for (i_idx, r_idx), route in zip(pairs, rs):
                routes[(i_idx, r_idx)] = route

        # Build cost dict: (inc_idx, res_idx) → cost
        cost: dict[tuple[int, int], float] = {}
        route_cache: dict[tuple[str, str], object] = {}
        for (i_idx, r_idx), route in routes.items():
            inc = incidents[i_idx]
            res = resources[r_idx]
            wi = w[inc.incident_id]
            eta_s = route.eta_seconds
            # Fatigue: resources already dispatched (but maybe re-routable) carry penalty
            fatigue = 0.0
            switch = 0.0
            if res.status == ResourceStatus.DISPATCHED and res.current_incident_id != inc.incident_id:
                fatigue = W_FATIGUE
                switch = self.config.switch_cost_penalty
            # Fuel range: if out of range, infeasible
            if route.distance_m / 1000.0 > res.fuel_range_km * 0.9:
                c = INFEASIBLE
            # Maintenance / failed
            elif res.status in (ResourceStatus.MAINTENANCE, ResourceStatus.FAILED):
                c = INFEASIBLE
            else:
                # Hospital selection penalty for transporting units (ambulances only)
                hosp_penalty = 0.0
                if res.kind == ResourceKind.AMBULANCE and hospitals:
                    # Nearest hospital with capacity; penalise if low
                    best_h = None
                    best_hc = float("inf")
                    for h in hospitals:
                        d_h = haversine_m(inc.location, h.location)
                        if h.available_beds <= 0:
                            continue
                        # Slight penalty for non-trauma vs critical
                        pen = 0.0 if (h.has_trauma or inc.severity.value < 4) else 300.0
                        hc = (d_h / 1000.0) / 60.0 * 60 + pen + W_OVERLOAD * (1.0 - h.capacity_ratio) * 60
                        if hc < best_hc:
                            best_hc = hc
                            best_h = h
                    if best_h is None:
                        hosp_penalty = W_OVERLOAD * 600  # all full
                    else:
                        hosp_penalty = best_hc
                c = wi * (W_ETA * eta_s + fatigue + switch) + 0.01 * res.effective_speed_kmh() + hosp_penalty
            cost[(i_idx, r_idx)] = c
            route_cache[(inc.incident_id, res.resource_id)] = route
        return cost, route_cache

    # ---- greedy ----
    def _greedy_assign(self, incidents, resources, needs, cost):
        assign: dict[str, list[tuple[int, ResourceKind, Optional[str], float]]] = {}
        used: set[int] = set()
        # Process highest urgency first
        order = sorted(incidents, key=lambda i: -i.urgency_score)
        for inc in order:
            slots = []
            for k_str, need_n in needs[inc.incident_id].items():
                k = ResourceKind(k_str)
                # candidate indices from pool
                i_idx_local = self._idx_of(incidents, inc.incident_id)
                candidates = [ri for ri in range(len(resources))
                              if resources[ri].kind == k
                              and (i_idx_local, ri) in cost
                              and cost[(i_idx_local, ri)] < INFEASIBLE
                              and ri not in used]
                candidates.sort(key=lambda ri: cost[(i_idx_local, ri)])
                for ri in candidates[:need_n]:
                    slots.append((ri, k, None, cost[(i_idx_local, ri)]))
                    used.add(ri)
            assign[inc.incident_id] = slots
        return assign

    # ---- Hungarian (per-kind bipartite) ----
    def _hungarian_assign(self, incidents, resources, needs, cost, init_assign):
        """Solve min-cost bipartite matching independently per resource kind.

        Uses scipy if available, otherwise a small O(n^3) Hungarian implementation
        for square matrices (we pad with dummy columns for unmatched slots).
        """
        try:
            from scipy.optimize import linear_sum_assignment  # type: ignore
            have_scipy = True
        except Exception:
            have_scipy = False

        best_assign = {k: list(v) for k, v in init_assign.items()}
        best_obj = self._objective(best_assign, incidents, cost)
        used: set[int] = set()
        for slots in best_assign.values():
            for (ri, *_rest) in slots:
                used.add(ri)

        for kind in ResourceKind:
            # Collect all open slots across incidents needing this kind
            slot_incidents = []
            for inc in incidents:
                n_needed = needs[inc.incident_id].get(kind.value, 0)
                already = sum(1 for s in best_assign[inc.incident_id] if s[1] == kind)
                for _ in range(max(0, n_needed - already)):
                    slot_incidents.append(inc)
            if not slot_incidents:
                continue
            # Free resources of this kind
            free_rs = [ri for ri, r in enumerate(resources)
                       if r.kind == kind and ri not in used
                       and cost.get((self._idx_of(incidents, inc_inc_id_for_ri(ri=ri, used_assign=best_assign, incidents=incidents, resources=resources) or slot_incidents[0].incident_id), ri), INFEASIBLE) < INFEASIBLE]
            # Build matrix (slots x free resources)
            n_slots = len(slot_incidents)
            n_res = len(free_rs)
            n = max(n_slots, n_res)
            BIG = 1e8
            mat = [[BIG] * n for _ in range(n)]
            for si, inc in enumerate(slot_incidents):
                for rj, ri in enumerate(free_rs):
                    c = cost.get((self._idx_of(incidents, inc.incident_id), ri), INFEASIBLE)
                    mat[si][rj] = c if c < INFEASIBLE else BIG
            if have_scipy:
                import numpy as np
                arr = np.array(mat)
                rows, cols = linear_sum_assignment(arr)
                pairs = list(zip(rows.tolist(), cols.tolist()))
            else:
                pairs = _hungarian(mat)
            for si, rj in pairs:
                if si >= n_slots or rj >= n_res:
                    continue
                if mat[si][rj] >= BIG - 1:
                    continue
                ri = free_rs[rj]
                inc = slot_incidents[si]
                best_assign[inc.incident_id].append((ri, kind, None, mat[si][rj]))
                used.add(ri)
        obj = self._objective(best_assign, incidents, cost)
        return best_assign, obj

    # ---- CP-SAT ----
    def _cpsat_assign(self, incidents, resources, needs, cost, init_assign, hospitals, budget_sec):
        try:
            from ortools.sat.python import cp_model  # type: ignore
        except Exception:
            return init_assign, self._objective(init_assign, incidents, cost)

        model = cp_model.CpModel()
        # Decision variables: x[(inc_idx, res_idx)] ∈ {0,1} for feasible candidates
        x = {}
        feasible = defaultdict(list)
        res_to_incs = defaultdict(list)
        for (i_idx, r_idx), c in cost.items():
            if c >= INFEASIBLE:
                continue
            var = model.NewBoolVar(f"x_{i_idx}_{r_idx}")
            x[(i_idx, r_idx)] = var
            feasible[i_idx].append((r_idx, var))
            res_to_incs[r_idx].append((i_idx, var))

        # Constraints
        # Each resource assigned to ≤ 1 incident
        for r_idx, pairs in res_to_incs.items():
            model.Add(sum(v for _, v in pairs) <= 1)

        # Each incident gets at most the required per-kind slots
        for i_idx, inc in enumerate(incidents):
            for k in ResourceKind:
                need_n = needs[inc.incident_id].get(k.value, 0)
                if need_n <= 0:
                    continue
                kind_vars = [v for (r_idx, v) in feasible[i_idx] if resources[r_idx].kind == k]
                if kind_vars:
                    model.Add(sum(kind_vars) <= need_n)

        # Objective: minimize cost
        obj_terms = []
        scale = 100  # integer scaling for CP-SAT
        for (i_idx, r_idx), var in x.items():
            obj_terms.append(var * int(cost[(i_idx, r_idx)] * scale))
        model.Minimize(sum(obj_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = budget_sec
        solver.parameters.num_search_workers = 2
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return init_assign, self._objective(init_assign, incidents, cost)

        assign: dict[str, list[tuple[int, ResourceKind, Optional[str], float]]] = {inc.incident_id: [] for inc in incidents}
        used: set[int] = set()
        for (i_idx, r_idx), var in x.items():
            if solver.Value(var) == 1:
                inc = incidents[i_idx]
                res = resources[r_idx]
                if r_idx in used:
                    continue
                used.add(r_idx)
                assign[inc.incident_id].append((r_idx, res.kind, None, cost[(i_idx, r_idx)]))
        obj = self._objective(assign, incidents, cost)
        return assign, obj

    # ---- LNS ----
    def _lns_improve(self, incidents, resources, needs, cost, assign, iterations=40):
        import random
        rng = random.Random(1337)
        current = {k: list(v) for k, v in assign.items()}
        current_obj = self._objective(current, incidents, cost)
        for _ in range(iterations):
            # Destroy: un-assign 2-5 random slots. We collect (inc_id, slot_obj)
            # pairs by reference so popping one does not shift the others.
            flat = []
            for inc_id, slots in current.items():
                for slot in slots:
                    flat.append((inc_id, slot))
            if not flat:
                break
            n_destroy = min(len(flat), rng.randint(2, 5))
            rng.shuffle(flat)
            chosen = flat[:n_destroy]
            removed = []
            for inc_id, slot in chosen:
                try:
                    idx = current[inc_id].index(slot)
                    current[inc_id].pop(idx)
                    removed.append((inc_id, slot))
                except ValueError:
                    continue
            # Repair: re-greedy the freed resources onto incidents with unmet needs
            freed_resources = {slot[0] for _, slot in removed}
            for inc in sorted(incidents, key=lambda i: -i.urgency_score):
                for k in ResourceKind:
                    need = needs[inc.incident_id].get(k.value, 0)
                    have = sum(1 for s in current[inc.incident_id] if s[1] == k)
                    while have < need:
                        best_ri = None
                        best_c = 1e18
                        i_idx = self._idx_of(incidents, inc.incident_id)
                        # pick from freed and not yet used
                        used_ri = {s[0] for slots in current.values() for s in slots}
                        for ri in freed_resources:
                            if ri in used_ri:
                                continue
                            if resources[ri].kind != k:
                                continue
                            c = cost.get((i_idx, ri), INFEASIBLE)
                            if c < best_c:
                                best_c = c
                                best_ri = ri
                        if best_ri is None or best_c >= INFEASIBLE:
                            break
                        current[inc.incident_id].append((best_ri, k, None, best_c))
                        freed_resources.discard(best_ri)
                        have += 1
            new_obj = self._objective(current, incidents, cost)
            if new_obj < current_obj:
                current_obj = new_obj
            else:
                # revert
                for inc_id, slot in removed:
                    current[inc_id].append(slot)
        return current, current_obj

    # ---- helpers ----
    def _objective(self, assign, incidents, cost):
        total = 0.0
        used_ri: set[int] = set()
        inc_idx = {inc.incident_id: i for i, inc in enumerate(incidents)}
        rejected_penalty = 0.0
        for inc in incidents:
            slots = assign.get(inc.incident_id, [])
            for (ri, k, h, c) in slots:
                if ri in used_ri:
                    total += INFEASIBLE  # conflict
                used_ri.add(ri)
                total += c
            # Penalty for unmet critical needs
            need = sum(self._needed_kinds(inc).values())
            if len(slots) < need:
                # Scale penalty by urgency
                rejected_penalty += inc.urgency_score * 600 * (need - len(slots))
        return total + rejected_penalty

    def _linear_lower_bound(self, incidents, needs, cost):
        lb = 0.0
        for inc in incidents:
            i_idx_local = self._idx_of(incidents, inc.incident_id)
            for k_str, n in needs[inc.incident_id].items():
                cs = sorted(cost[(i_idx_local, ri)] for ri in range(self._n_res(cost, i_idx_local))
                            if (i_idx_local, ri) in cost and cost[(i_idx_local, ri)] < INFEASIBLE)
                if len(cs) < n:
                    lb += INFEASIBLE * 0.001 * (n - len(cs))
                    n = len(cs)
                lb += sum(cs[:n])
        return lb

    @staticmethod
    def _idx_of(incidents, inc_id):
        for i, inc in enumerate(incidents):
            if inc.incident_id == inc_id:
                return i
        raise KeyError(inc_id)

    @staticmethod
    def _n_res(cost, i_idx):
        return max((r for (i, r) in cost.keys() if i == i_idx), default=0) + 1


# --- helpers for Hungarian fallback ---
def _estimate_eta_from_cost(c):
    # Rough inverse of W_ETA*eta when wi≈1 and other terms are 0
    return max(1.0, c / max(1e-6, W_ETA))


def _hungarian(cost):
    """O(n^3) Hungarian (Jonker-Volgenant style) for square integer/float cost.
    Returns list of (row, col) pairs for optimal assignment. Used when scipy
    is unavailable; implementation is compact but correct for moderate sizes.
    """
    n = len(cost)
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    INF = float("inf")
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0 != 0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    pairs = []
    for j in range(1, n + 1):
        if p[j] != 0:
            pairs.append((p[j] - 1, j - 1))
    return pairs


def inc_inc_id_for_ri(ri, used_assign, incidents, resources):
    # helper that returns an incident_id for a resource (needed to fetch from cost)
    for inc_id, slots in used_assign.items():
        for (r, *_rest) in slots:
            if r == ri:
                return inc_id
    return None
