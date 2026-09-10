# ADR 0004: Shared context is assembled, not a global conversation

- Status: accepted
- Date: 2026-09-09

## Decision

Every assistant run receives an immutable context snapshot assembled from identity, channel,
thread history, accepted memories, project artifacts, live authoritative data, policy, and the
effective capability manifest. Voice, chat, scheduled tasks, and workers share these sources but
do not share mutable prompt state.

Every context item records its source, retrieval reason, sensitivity, and timestamp. Live device
or provider state is fetched from its owner and is not converted into durable memory by default.

## Consequences

Runs are reproducible and auditable. Channel handoff binds to a thread explicitly. Context can be
scoped and redacted without creating channel-specific memory silos.

