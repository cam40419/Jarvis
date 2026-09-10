# Phase 1 readiness and build order

Status: ready to begin after the database gate below passes.

## Foundation gates

- [x] Replacement code is isolated under `src/jarvis`.
- [x] Legacy runtime and obsolete dependency list are removed.
- [x] Domain contracts reject unknown fields and use timezone-aware timestamps.
- [x] Capability execution is scoped, validated, audited, and atomically idempotent.
- [x] Job transitions are explicit and use optimistic versions.
- [x] Hard and dangerous writes fail closed.
- [x] Production startup refuses the development identity adapter.
- [x] CI enforces linting, strict types, tests, and at least 90% coverage.
- [ ] The initial migration has executed successfully against PostgreSQL with pgvector.
- [ ] PostgreSQL adapters pass the reference adapter contract suite.
- [ ] Backup and restore have been exercised with documented recovery results.

No live integration or model adapter should merge until the remaining database gates pass.

## Phase 1 implementation order

1. **Persistence:** migration runner, PostgreSQL transaction manager, stores, contract tests, and
   outbox publisher.
2. **Identity:** WebAuthn/passkey relying-party design, sessions, CSRF protection, membership and
   scope resolution, and a development-login replacement.
3. **Threads and runs:** message persistence, immutable run records, context-item provenance, and
   streaming run events.
4. **Context v1:** recent-thread window, deterministic summary interface, accepted explicit
   memories, scope filtering, and token budgeting.
5. **Model boundary:** fake model contract tests followed by one Responses API adapter. Provider
   conversation identifiers are optimization metadata, not canonical history.
6. **Capabilities:** persistent catalog, manifest builder, tool-call loop, output validation,
   timeouts, and adapter health.
7. **Calendar read-only:** isolated OAuth credentials, minimal scopes, normalized events, and no
   write capability.
8. **PWA shell:** passkey login, thread view, streaming output, job status, audit visibility, and
   error recovery.
9. **Operational gate:** tracing, redaction checks, rate limits, dependency health, restore test,
   and end-to-end threat scenarios.

## Required scenario tests

- A user cannot access another household by changing a path, header, or object identifier.
- A replayed request does not repeat model-independent side effects.
- Two workers cannot successfully lease the same job.
- A crashed worker's expired lease is safely recoverable.
- A transaction rollback publishes no outbox event.
- A malformed model tool call cannot reach a handler.
- A capability removed between discovery and invocation is denied.
- Untrusted calendar text cannot alter scopes or invoke a write.
- Stream reconnection resumes from an event cursor without duplicating persisted messages.
- Secrets and sensitive context do not appear in logs, traces, or API errors.

## Explicitly deferred

Voice, proactive actions, Home OS device writes, Codex workers, and semantic memory extraction remain
out of Phase 1's first vertical slice. Their contracts may be drafted, but no live authority is
enabled until identity, persistence, and auditing have passed their gates.

