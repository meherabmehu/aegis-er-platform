# ADR-0001 — Event-driven microservices on Kafka

## Status
Accepted.

## Context
The system must ingest high-throughput, continuously-arriving events, coordinate many stateful services, support replay for audit/ML, and degrade gracefully during network partitions.

## Decision
We adopt an event-driven microservices architecture with Apache Kafka (or Redpanda, API-compatible) as the universal event bus. Services communicate via immutable, typed events on partitioned topics.

## Consequences
- **+** Natural back-pressure, replay, audit, and decoupled scaling.
- **+** CQRS and event sourcing become straightforward.
- **+** Multi-region mirroring (MirrorMaker 2) enables edge autonomy.
- **−** Operational complexity; mitigated with managed Kafka (e.g., MSK/Confluent Cloud) and schema-managed Protobuf/Avro contracts.
- **−** Eventual consistency — resolved with Saga + outbox patterns and idempotent consumers.
