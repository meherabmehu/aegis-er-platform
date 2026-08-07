# ADR-0004 — Saga + outbox for dispatch orchestration

## Status
Accepted.

## Context
Dispatching a unit atomically updates the resource state, writes a dispatch record, emits an event, and pushes a notification. Distributed 2PC across Postgres+Kafka+push services would be operationally fragile.

## Decision
We use the Saga pattern with a transactional outbox (in Postgres): a dispatch and its resource-reservation rows commit in a single local transaction; a relay forwards the outbox events to Kafka. Compensating actions (release reservation, reroute to next hospital, mark REJECTED) fire if any step fails.

## Consequences
- **+** No distributed transactions; survives partial failures cleanly.
- **+** Guaranteed at-least-once delivery; idempotent consumers prevent duplicates.
- **−** Business logic must be written with compensating actions in mind; mitigated by a typed Saga DSL and tests for every failure scenario.
