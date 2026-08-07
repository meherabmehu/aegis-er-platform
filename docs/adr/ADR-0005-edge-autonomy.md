# ADR-0005 — Edge-autonomy mode for regional clusters

## Status
Accepted.

## Context
Disasters often disrupt communications. If the core or inter-region WAN goes down, emergency response must continue locally.

## Decision
Each regional cluster maintains a local solver and read replica of its region's state. On WAN partition, the cluster transitions to edge-autonomy mode: it continues to dispatch using cached state and conservative heuristics (haversine routing, default capacities), and reconciles state with the core by replaying Kafka events when connectivity heals.

## Consequences
- **+** Lifesaving continuity during network failures.
- **+** CRDT-like last-write-wins with vector clocks for resource status avoids split-brain conflicts on heal.
- **−** Slight risk of duplicate dispatches during extreme partitions; mitigated by idempotent dispatch IDs and post-heal reconciliation that cancels duplicates.
