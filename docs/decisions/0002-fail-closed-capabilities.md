# ADR 0002: Capabilities are scoped and fail closed

- Status: accepted
- Date: 2026-09-09

## Decision

Every tool is a versioned capability with strict schemas, required scopes, a risk class, and
idempotency behavior. Discovery filters capabilities for usability, but authorization is repeated
at execution. `write_hard` and `dangerous` operations remain disabled until an action-bound signed
confirmation verifier is implemented and reviewed.

## Consequences

No model, channel, MCP server, or worker can grant itself authority. Adding a capability requires
an explicit contract and policy classification. Untrusted content cannot broaden the manifest.
