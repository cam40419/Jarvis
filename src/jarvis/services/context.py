import json
from collections.abc import Sequence
from typing import Protocol

from jarvis.domain.context import ContextPolicy, ExplicitMemory, MemoryContext, SummaryContext
from jarvis.domain.conversations import ContextItem, Message
from jarvis.domain.errors import ValidationError


class Summarizer(Protocol):
    def summarize(self, messages: Sequence[Message]) -> SummaryContext | None: ...


class ExcerptSummarizer:
    """Bounded, verbatim excerpts; makes no claim to summarize the whole conversation."""

    def summarize(self, messages: Sequence[Message]) -> SummaryContext | None:
        if not messages:
            return None
        return SummaryContext(
            source_message_ids=tuple(m.id for m in messages),
            source_sequences=tuple(m.sequence for m in messages),
            text="\n".join(f"{m.sequence} {m.role}: {m.text[:80]}" for m in messages),
        )


def context_cost(
    messages: Sequence[ContextItem],
    memories: Sequence[MemoryContext],
    summary: SummaryContext | None,
) -> int:
    # Conservative local accounting, not a provider tokenizer or total prompt measurement.
    payload = {
        "messages": [m.model_dump(mode="json") for m in messages],
        "memories": [m.model_dump(mode="json") for m in memories],
        "summary": summary.model_dump(mode="json") if summary else None,
    }
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class ContextAssembler:
    def __init__(self, summarizer: Summarizer | None = None) -> None:
        self.summarizer = summarizer or ExcerptSummarizer()

    def assemble(
        self, history: Sequence[Message], current: Message, memories: Sequence[ExplicitMemory]
    ) -> tuple[
        tuple[ContextItem, ...], tuple[MemoryContext, ...], SummaryContext | None, ContextPolicy
    ]:
        def item(message: Message) -> ContextItem:
            return ContextItem(
                source_message_id=message.id,
                source_sequence=message.sequence,
                role=message.role,
                text=message.text,
            )

        budget = 24000 - 4096
        selected = [item(current)]
        memory_items: list[MemoryContext] = []
        if context_cost(selected, (), None) > budget:
            raise ValidationError("message exceeds context budget")
        # Keep complete adjacent turns, newest first, and never skip a gap in recent history.
        recent = history[-16:]
        for end in range(len(recent), 1, -2):
            pair = [item(m) for m in recent[end - 2 : end]]
            if context_cost([*pair, *selected], (), None) > budget:
                break
            selected = [*pair, *selected]
        for memory in memories:
            candidate = MemoryContext(
                source_memory_id=memory.id, subject=memory.subject, text=memory.content
            )
            if context_cost(selected, [*memory_items, candidate], None) <= budget:
                memory_items.append(candidate)
        selected_ids = {m.source_message_id for m in selected}
        older = [m for m in history if m.id not in selected_ids]
        summary = self.summarizer.summarize(older)
        if context_cost(selected, memory_items, summary) > budget:
            summary = None
        represented = len(selected) - 1 + (len(summary.source_message_ids) if summary else 0)
        policy = ContextPolicy(
            used=context_cost(selected, memory_items, summary),
            omitted_messages=current.sequence - 1 - represented,
            memory_candidates=len(memories),
            omitted_memories=len(memories) - len(memory_items),
        )
        return tuple(selected), tuple(memory_items), summary, policy
