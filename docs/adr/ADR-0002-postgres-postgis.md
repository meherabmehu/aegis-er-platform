# ADR-0002 — PostgreSQL + PostGIS as system of record

## Status
Accepted.

## Context
Dispatch correctness is legally/medically sensitive; geospatial queries (nearest resource, route pre-filter, hospital catchment) are on the hot path.

## Decision
PostgreSQL 16 with PostGIS is the source of truth for incidents, resources, dispatches, and hospitals. Redis is used only as a cache/lock store, never a source of truth. Other stores (TimescaleDB, Elasticsearch) derive from the Kafka event log.

## Consequences
- **+** ACID transactions, mature geospatial indexing (GiST), operational familiarity.
- **+** Avoids dual-write races; all writes go to Postgres and fan out through the outbox/Kafka.
- **−** Geo-sharding adds operational complexity — mitigated by per-region primaries with async global aggregates for analytics.
