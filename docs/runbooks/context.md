# Bounded conversation context

Each run records the messages, memory entries and excerpts selected for its model
context. The chat inspector and `GET /v1/runs/{id}` expose that recorded provenance.
Current personal memory and cross-conversation retrieval are described in
[shared context](shared-context.md).

The `context-v1` assembler starts from a bounded recent-message window, prioritizes
the current input and complete recent turns, and adds accepted memories when they
fit. Its deterministic excerpts are partial quotations, not semantic summaries of
all conversation history. Omitted counts remain visible in the snapshot.

The local context budget uses UTF-8 byte length, including provenance: 24,000 units
with 4,096 reserved. This is not an exact provider token count. The model adapter
separately bounds its final input, including instructions and tool definitions.

All retrieved conversation, memory and document text is untrusted content. It
cannot grant executable permissions or replace system instructions. Retraction
removes memory from future selections; previous run snapshots retain the content
they actually used. Audit and outbox records identify resources without copying
memory text. See [storage recovery](storage-recovery.md) for backup boundaries.
