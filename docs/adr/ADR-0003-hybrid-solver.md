# ADR-0003 — Hybrid anytime solver (greedy → Hungarian → CP-SAT → LNS)

## Status
Accepted.

## Context
Dispatch decisions must be near-optimal *and* fast (< 200 ms p95) under highly dynamic inputs; side constraints (hospital capacity, fuel, crew hours, terrain, skills) make the problem NP-hard; mass-casualty events create very large batches.

## Decision
We implement an anytime hybrid solver: (1) greedy warm-start guarantees feasibility; (2) Hungarian/JV gives optimal linear-sum matching; (3) OR-Tools CP-SAT adds side constraints within a wall-clock budget; (4) LNS continues improving. Every step falls back gracefully if dependencies are missing or the budget expires.

## Consequences
- **+** Always returns a feasible plan within a hard deadline.
- **+** Optimality improves the longer the budget (during mass-casualty lulls).
- **+** Graceful degradation: if OR-Tools is unavailable the greedy+Hungarian path still works.
- **−** Multiple solving paths raise test surface — mitigated with property-based tests (Hypothesis) ensuring every phase returns constraint-respecting plans.
