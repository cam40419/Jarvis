# Conversations and deterministic runs

## Browser test

For the deterministic checks below, start Docker Desktop, then run
`./scripts/start-dev.ps1 -TestRunner` from PowerShell. For real answers omit `-TestRunner` and
follow [using the assistant](assistant.md). The launcher applies
migration 0003. For first-time passkey setup use `-Enroll`; for a disposable login token use
`-DevelopmentLogin`. Open http://localhost:8000/login and sign in, then select **Open conversations**.

1. Enter a title and select **Create conversation**.
2. Send a message. Expect your message and `Test runner received: ...`, followed by
   **Run complete. Messages saved.**
3. Reload the page. Both messages should remain.
4. Stop the API with Ctrl+C, run the launcher again with the same flags, and reopen the conversation. PostgreSQL
   preserves the messages and session. The `-Memory` launcher intentionally loses both on restart.

No model key is required. Owner and member roles share conversations within their active household;
guests have no conversation access. Use the Account page to switch households, then reopen chat.
All message text is displayed as text, including HTML-looking input.

## API and reconnect checks

Authenticate through the browser or `/auth/dev-login` as documented in the identity runbook.
Keep the session cookie; POSTs also need the exact Origin and session CSRF header.
The interactive request schemas are available at http://localhost:8000/docs.

| Endpoint | Request / result |
| --- | --- |
| POST `/v1/threads` | `{ "title": "Test", "idempotency_key": "thread-test-001" }` |
| GET `/v1/threads?offset=0&limit=50` | Current household threads, ordered by creation time and ID |
| GET `/v1/threads/{id}` | One scoped thread |
| POST `/v1/threads/{id}/runs` | `{ "text": "Hello", "idempotency_key": "run-test-001" }` |
| GET `/v1/threads/{id}/messages?after=0&limit=100` | Messages with sequence greater than `after` |
| GET `/v1/runs/{id}` | Recorded context sources, input/output IDs, configuration and outcome |
| GET `/v1/runs/{id}/events` | Four persisted SSE events, numbered 1 through 4 |

Repeat the same POST and key: the IDs and records remain identical. Reuse that key with changed
content: expect 409. Keys are scoped to actor and household, and run keys also to the thread.
Thread/message page size is capped at 100; iterate thread offsets to retrieve additional pages.
The initial browser page shows up to 100 threads.

Request the event endpoint with `Last-Event-ID: 2`: only events 3 and 4 return. The header takes
precedence over the alternative `?after=2` query. Cursor 4 returns HTTP 204 so EventSource stops
reconnecting. Negative, malformed, or future cursors return 422. Events contain message IDs;
read canonical message content from the message endpoint. Close EventSource on `run.completed`.
This follows the [HTML server-sent events protocol](https://html.spec.whatwg.org/multipage/server-sent-events.html).

## Implementation boundaries

This is a synchronous test runner, not live model-token generation. Creation of both messages,
the final run snapshot, four events, idempotency response, audit and outbox intent occurs in one
transaction. A failure commits none of them; retry can safely execute again. Event streaming
only reads committed state and holds no transaction while sending bytes. Authentication and
membership are resolved on every request, including reconnection. Streams are finite and short.

Snapshots contain selected prior messages plus current input, source IDs/sequences,
an untrusted-content label, and pinned runner/prompt versions. Context v1 also records selected
memories, excerpts, and omission/budget accounting. The executable capability manifest
is empty because this runner executes no tools. No conversation content grants authority.
Conversation text is stored in messages, snapshots, and idempotency results; audit/outbox payloads
contain identifiers only. Normal database backup/access controls apply to these sensitive records.

Context v1 now selects bounded history, excerpts and explicit memories, recording omitted counts
and budget usage; threads can continue beyond 50 turns. See [Context v1](context.md).
Asynchronous status changes, model failures,
cancellation and worker recovery are not implemented. SQL triggers prohibit rewriting/deleting
messages, events and completed snapshots; a future retention policy requires an explicit migration.

## Automated verification

```powershell
$env:JARVIS_TEST_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test'
$env:JARVIS_BROWSER_TESTS = '1'
$env:JARVIS_BROWSER_CHANNEL = 'msedge'
.\venv\Scripts\python.exe -m pytest -q --cov=jarvis --cov-report=term-missing
.\venv\Scripts\python.exe -m ruff check src tests scripts
.\venv\Scripts\python.exe -m mypy src
```

The optional browser test requires `pip install -e ".[dev,postgres,browser]"` in the venv and Edge
on Windows. Without Edge, install Playwright Chromium and omit `JARVIS_BROWSER_CHANNEL`.
Tests cover both stores, cross-household denial, concurrent retries, rollback, context provenance,
cursor replay, SQL immutability, real API process restart, and browser send/reload with passkey login.

Verified locally on September 10, 2026: **118 tests passed, 97.02% coverage**, Ruff and strict mypy
passed, and the wheel built successfully. Migration 0003 is applied to the development database.
The restore drill matched all 20 public tables; its archive and JSON report are
`.local/backups/jarvis_20260911T022629Z_a4eae9f9.*` (UTC filename). The test client emits one upstream
Starlette/httpx deprecation warning; it does not affect the passing checks.
