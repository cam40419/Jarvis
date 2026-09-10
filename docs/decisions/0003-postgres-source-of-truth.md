# ADR 0003: PostgreSQL is the initial durable source of truth

- Status: accepted
- Date: 2026-09-09

## Decision

PostgreSQL stores conversations, runs, memories, grants, jobs, idempotency records, confirmations,
audit records, and the transactional outbox. pgvector provides the initial semantic index. Object
storage will hold large artifacts. A message broker may be added for delivery and fan-out, but it
will not become authoritative state.

Job workers will claim work with leases and row locking. Mutating operations and their outbox
events must commit in the same transaction. Database adapters must pass the same behavioral
contract suite as the in-memory reference adapter.

## Consequences

The first deployment has one primary persistence technology and one backup/restore path. High-rate
telemetry and artifact storage can split later without changing the core contracts.

