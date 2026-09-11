# Phase 1 readiness and build order

Status: persistence, identity, and the deterministic threads/runs milestone are implemented.
Context v1 and the Responses API text assistant are implemented. Tool integration, worker execution,
and operational hardening remain outstanding.

## Foundation gates

- [x] Replacement code is isolated under `src/jarvis`.
- [x] Legacy runtime and obsolete dependency list are removed.
- [x] Domain contracts reject unknown fields and use timezone-aware timestamps.
- [x] Capability execution is scoped, validated, audited, and atomically idempotent.
- [x] Job transitions are explicit and use optimistic versions.
- [x] Hard and dangerous writes fail closed.
- [x] Production configuration requires HTTPS, PostgreSQL, and development login disabled.
- [x] CI enforces linting, strict types, tests, and at least 90% coverage.
- [x] The initial migration has executed successfully against PostgreSQL with pgvector.
- [x] PostgreSQL adapters pass the reference adapter contract suite.
- [x] Backup and restore have been exercised with documented recovery results.

Evidence and repeatable commands are in the [persistence runbook](runbooks/persistence.md).
The local restore drill compared all 15 public tables, including seeded identity, a submitted
job, an echo replay record, audit records, and pending outbox events. It retained a binary
archive and machine-readable report under `.local/backups`.
After the identity migration, a second restore drill matched all 19 public tables. The
readable foundation SQL is retained under `db/schema`; applied migration bytes remain immutable.

Persistence now includes a migration runner, shared transactions, PostgreSQL stores for jobs,
idempotency, audits and outbox, and an at-least-once outbox publisher callback. The capability
handler registry remains code-local. Worker leases and recovery are not implemented; the
worker scenario tests below remain future gates. Passkeys and sessions now provide the
identity boundary. Real model access now uses bounded, recorded conversation context.

## Identity gates

- [x] Self-asserted actor/household/scope headers have been removed as an authentication mechanism.
- [x] Discoverable passkey enrollment and authentication verify challenge, RP, origin, UV, and signature.
- [x] Enrollment tokens and browser-bound ceremonies expire and cannot be replayed.
- [x] Sessions persist, rotate, expire, and can be revoked; only token hashes are stored.
- [x] Membership and role resolution happen on every protected request.
- [x] Mutations require the exact Origin and a session-bound CSRF token.
- [x] Local operator enrollment, membership, and revocation commands are available and audited.
- [x] Browser enrollment, login, authenticated echo, reload, and logout passed with a virtual authenticator.

See the [identity runbook](runbooks/identity.md) and [ADR 0005](decisions/0005-passkey-sessions.md).
The physical Windows Hello/security-key prompt is an interactive user test; automated verification
uses generated keys and an isolated browser profile, not the user's personal credentials.

## Threads and runs gates

- [x] Household-scoped threads and ordered user/assistant messages persist in PostgreSQL.
- [x] Run snapshots record immutable input provenance and runner configuration.
- [x] Duplicate and concurrent retries create one turn, one run, and one audit/outbox intent.
- [x] SSE resumes after Last-Event-ID without duplicating persisted messages.
- [x] Browser send/reload and API process restart preserve conversation history.
- [x] Completed snapshots, messages, and event rows reject database updates and deletes.

The deterministic runner completes atomically before streaming recorded events. Live model token
streaming, asynchronous run states, cancellation, and crash recovery for workers remain future work.
Context v1 removes the original 50-turn limit while bounding each run's selected context.
See the [conversation runbook](runbooks/conversations.md).

## Context v1 gates

- [x] Bounded recent-message retrieval preserves complete turns and current input.
- [x] Deterministic summary interface produces labelled, source-linked excerpts.
- [x] Explicit household memories require a deliberate save action and support retraction.
- [x] Scope checks apply to retrieval and reading/replaying snapshots containing memory.
- [x] Immutable snapshots record budget accounting and omitted-item counts.
- [x] Existing snapshots remain readable and retain their original data.

The budget uses a documented byte-count proxy; the model adapter must enforce real token limits.
Summaries are bounded excerpts, not cumulative semantic summaries. See the
[context runbook](runbooks/context.md) for limits and test instructions.

## Model boundary gates

- [x] Fake-model contracts and mocked real-SDK HTTP tests cover requests and failures.
- [x] Responses API adapter counts input tokens, caps output, and disables automatic retries.
- [x] Generation occurs outside database transactions with durable attempts and duplicate protection.
- [x] Completed runs record the exact prompt/input, returned model, response ID, and usage.
- [x] Access is revalidated before publishing answers.
- [x] Launcher and browser expose real assistant mode and retain offline echo as an option.

See [using the assistant](runbooks/assistant.md). Token streaming, tools, and async worker recovery
remain future work. A crash can leave the provider outcome unknown; retries do not automatically
regenerate or guarantee exactly-once billing.

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

