# Conversations and runs

Sign in and open **Chat**. New conversations are private to their creator within
the active workspace. Current membership is checked on reads and writes. Guests
cannot access chat. The native project board is a separate authority: chat messages
do not create projects, staff teams or start board tasks.

The configured model path and optional durable background chat worker are described
in [using the assistant](assistant.md). For an offline echo test, start
`scripts/start-dev.ps1 -TestRunner`; this tests persistence without a provider call.
The memory backend loses state on restart; PostgreSQL preserves records.

| Endpoint                        | Purpose                                                 |
| ------------------------------- | ------------------------------------------------------- |
| `POST /v1/threads`              | Create a conversation with title and idempotency key    |
| `GET /v1/threads`               | Page visible conversations                              |
| `GET /v1/threads/{id}/messages` | Page canonical messages                                 |
| `POST /v1/threads/{id}/runs`    | Submit text using the configured synchronous runner     |
| `GET /v1/runs/{id}`             | Read recorded input, context, configuration and outcome |
| `GET /v1/runs/{id}/events`      | Replay persisted events with a cursor                   |
| `/v1/work-sessions`             | Durable background chat requests and their controls     |

Use `/docs` for current request schemas. Session mutations require the configured
Origin and CSRF token. Idempotency keys are scoped to the actor and workspace, and
run keys also to their thread. Changed content under a reused key returns a conflict.
Membership and visibility are rechecked before reading saved records or replaying
events. An earlier authorization does not grant permanent access.

Messages and run snapshots contain sensitive conversation content. Assistant text
uses a restricted Markdown renderer; embedded HTML is displayed as text. Context
sources are labelled untrusted. See [bounded context](context.md), [identity](identity.md)
and [database testing](database-testing.md) for the corresponding boundaries.
