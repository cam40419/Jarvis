# Context v1

## Try it

Run `./scripts/start-dev.ps1`, sign in at http://localhost:8000/login, and open conversations.
The launcher applies migration 0004 to add explicit memory storage.

1. Expand **Shared household memories**. Enter a subject and fact/preference, then select
   **Save shared memory**. Saving explicitly accepts it for use in future runs.
2. Send a message. Expand **Last run context** to inspect the exact message sources, memory
   sources, summary excerpts, and budget accounting selected for this run.
3. Retract the memory and send another message. It disappears from the new context. Earlier
   immutable run snapshots retain their recorded copies.
4. Continue past 50 turns. Messages remain in canonical history; each run selects bounded context.
   The browser loads history in pages, including messages beyond the first 100.

The default launcher now uses the real assistant, which can answer using selected memories.
Use `-TestRunner` for offline selection checks. The last-run inspector is transient; use
`GET /v1/runs/{id}` to retrieve a persisted snapshot after reloading.

## Selection policy

`context-v1` fetches at most the latest 32 messages and 100 active explicit household memories.
Within one household transaction it:

1. Includes the current input in full, or rejects an input that cannot fit.
2. Adds up to eight complete recent user/assistant turns, newest first, without skipping gaps.
3. Adds accepted explicit memories in creation-time/ID order when each fits the remaining budget.
4. Uses a deterministic summarizer interface to create labelled excerpts of fetched messages not
   included in full. Each excerpt contains the first 80 characters, role, sequence, and source ID.
   The excerpt bundle is omitted if it cannot fit.

Excerpts are partial quotations, not a semantic or cumulative summary of the entire thread.
Messages older than the retrieval window remain stored but are not summarized. The snapshot records
how many messages have no representation and how many candidate memories were omitted. An excerpt
represents only part of its source; its source is not counted as wholly omitted.

Budget accounting uses the UTF-8 byte length of serialized context, including provenance metadata:
24,000 units total, with 4,096 reserved for future prompt/output overhead. This is a deterministic
local proxy, **not an exact model token count or a guarantee for any provider's prompt format**.
The Responses adapter now counts the final provider input and enforces a separate token ceiling.
Policy version, estimator, allowance, reserve, and used units are captured in each new run.
Existing snapshots remain readable and return `context_policy: null` when they predate this policy.

## Memory permissions and lifecycle

Only household-shared, user-confirmed explicit memories are supported. Owner/member roles may read
and create them. Creators may retract their own entries; owners may retract any household entry.
Guests cannot read or write memory. No personal-only memory is injected into shared conversations.
The API rejects client-supplied scope, acceptance, sensitivity, actor, or household fields.

| Endpoint | Behavior |
| --- | --- |
| POST `/v1/memories` | Accept `{ "subject": "Dinner", "content": "Vegetarian", "idempotency_key": "memory-test-001" }` |
| GET `/v1/memories?offset=0&limit=100` | List active memories for the session household |
| POST `/v1/memories/{id}/retract` | Exclude the entry from future runs; repeat safely |

POSTs require the session cookie, exact Origin, and CSRF token. Creating memory is idempotent within
actor/household; a changed body under the same key returns 409. Replaying creation after retraction
returns the current retracted entry and does not reaccept it. Create a new entry/key to accept it again.
There is an explicit limit of 100 active memories per household; retract unused entries to free space.
Entries do not expire automatically in this phase. Editing is performed by retracting and replacing.

Memory creation/retraction, idempotency, audit, and outbox intents share one transaction. Audit/outbox
payloads contain IDs, not memory text. Retraction is not erasure: stored memory, past snapshots, and
backups retain content. Retention/erasure and private/project scope are future work.

The assembler only retrieves supported, accepted explicit entries from the current household and
only when the actor has `memories:read`. Reading or replaying a run containing memory also requires
that scope. All conversation, excerpt, and memory text stays labelled untrusted. It cannot change
the empty executable capability manifest, actor permissions, or system instructions.

## Verification

Use the full-suite commands from the conversation runbook. Context tests exercise both adapters,
Unicode budget pressure, complete-turn selection, deterministic excerpts, legacy snapshot decoding,
cross-household denial, scope loss on retry, rollback, concurrent memory creation, retraction,
API CSRF, process restart, and the browser save/inspect/retract flow.

Verified September 11, 2026: **134 tests passed, 97.28% coverage**; Ruff, strict mypy,
and wheel packaging passed. Migration 0004 is applied to the local development database.
Backup/restore matched all 20 public tables; archive and report:
`.local/backups/jarvis_20260911T130044Z_20e94e25.*`.
The existing Starlette/httpx test-client deprecation warning remains non-failing.

The model boundary is now implemented; see [using the assistant](assistant.md). Semantic retrieval,
automatic extraction, model-generated summaries, and long-term cumulative summaries remain deferred.
