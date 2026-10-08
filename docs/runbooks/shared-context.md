# Shared context: text, voice, and personal memory

## Try it

Run `./scripts/start-dev.ps1`, sign in at http://localhost:8000/login, and refresh the chat page.
The launcher applies current migrations. Each voice call receives bounded startup context.

1. Start a new chat and say: “My ongoing project is Atlas. It automates my Bambu A1 plate changer.”
   Simon can save useful durable context without requiring the word “remember” or a confirmation.
2. Open **Memory** to see saved facts, preferences, and projects. Personal entries are labelled
   **Personal**. The manual form defaults to **Only me**; **Shared workspace** is explicit.
3. Start a voice conversation and ask: “What do you remember about my Atlas project?” Then ask
   about a detail from another conversation. Voice delegates retrieval to the same backend as chat.
4. Tell voice a new project detail. After Simon acknowledges saving it, open a new text chat and
   ask about it. Reloading Simon or restarting the server preserves the saved context.
5. Correct a fact or ask to forget an identified memory. Corrections reuse its subject and
   supersede the old record; forgetting retracts it from active memory. The panel also supports
   retraction. Retraction does not erase original conversations, historical snapshots, or backups.

Existing text and saved voice transcripts are searchable immediately. There is no paid history
backfill, and past conversations are not automatically converted into structured facts. Facts
saved during conversation are selected by the model; extraction is not an exhaustive transcript
summary. Local `-TestRunner` mode remains a deterministic echo and does not extract memories.

## What Simon retrieves

- **Active memories:** personal facts, preferences, and projects, plus explicitly shared workspace
  memories. New text requests receive a bounded selection. Voice receives a bounded recent-memory
  snapshot when the call starts and delegates recall/corrections to retrieve current records.
- **Conversation recall:** keyword search over this account's text conversations and stored voice
  transcripts in the active workspace. Consecutive voice fragments from the same speaker are joined
  so words split between transcript events remain searchable. Voice backend wrappers are excluded
  to avoid duplicating a call's transcript.
- **Sources:** results identify the source conversation, date, role, and whether the excerpt is
  truncated. Historical statements are user-reported context, not proof of present device state.

`context_search` accepts short topic keywords and an offset. An empty query lists recent history;
`next_offset` pages conversation results. Searches rank by matched terms, then recency. It is
keyword retrieval, not an embedding index or exhaustive semantic search. A model can try another
keyword when a topic has different wording. The read-only API is `GET /v1/context/search?query=Atlas`.

Text requests include up to three relevant historical excerpts, excluding the current thread.
Voice starts with up to three recent excerpts and selected memories. The backend tool searches
older conversations on demand rather than sending all history into every voice call. This follows
the separation of live conversation and backend work described in the
[OpenAI Live prompting guide](https://developers.openai.com/api/docs/guides/live-prompting).

## Persistence and boundaries

New conversations, including voice-linked threads, are private to their creator.
Read APIs, run inspection, feedback and pagination enforce thread visibility.

Personal memories are scoped to both actor and workspace. Another workspace member,
including an owner, cannot list, use or retract them. The manual form and conversational
tools choose personal scope. The memory API accepts an explicit `personal` or `workspace`
scope and defaults to `workspace`; callers should choose deliberately. Accounts do not
share personal memory automatically. A memory categorized as `project` is a descriptive
context entry, not a native project, board or source of agent authority.

`memory_remember` stores a fact only against an active request belonging to the actor, with a quote
from that request as evidence. Earlier voice excerpts and tool/web results are not accepted as
the quote source. Instructions prohibit invented facts, transient commands, secrets, and details
the user asks not to save; sensitive details require an explicit request. Evidence checking proves
the quote's source, not the truth of the extracted fact, so users can inspect and correct memories.

Saving/updating is transactional, idempotent, and audited without memory content in audit payloads.
Updates create a replacement with `supersedes` and `source_message_id`; historical snapshots remain
immutable. A failed answer can still have saved a valid memory before generation failed. “Think
deeper” cannot write memories again. Tools revalidate access, current attempt state, and private
thread visibility before executing. Memory and historical text never grant tool permissions.

## Bounds and checks

The `context-v1` assembler retains at most eight recent complete text turns from the last
32 messages and fits memories/excerpts within its UTF-8 byte budget. It now considers up to 500
visible active memories; shared workspace entries retain their limit of 100. Additional recall
has separate bounded excerpts (4 KB in text startup, 3.5 KB in voice startup; up to eight excerpts
for a tool search). A voice memory snapshot is bounded to 6 KB. Provider input-token counting still
checks the entire backend request, including instructions and tool definitions.

Tests cover both storage adapters, existing text and fragmented voice recall, account/workspace
isolation, private thread access, evidence rejection, duplicate saves, correction/retraction,
voice startup and delegation, new-chat recall, API auth/validation, and the memory UI. The opt-in
paid test `tests/integration/test_live_recall.py` checks real-model extraction from a natural
statement and recall of saved text and synthetic voice history. Real human microphone recognition
and the live model's delegation behavior still require a spoken acceptance check.
