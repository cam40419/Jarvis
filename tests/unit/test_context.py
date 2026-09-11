from uuid import uuid4

import pytest

from jarvis.domain.context import ExplicitMemory
from jarvis.domain.conversations import Message
from jarvis.domain.errors import ValidationError
from jarvis.services.context import ContextAssembler, ExcerptSummarizer, context_cost


def history(count, text="Hello"):
    thread_id = uuid4()
    return tuple(
        Message(
            thread_id=thread_id,
            sequence=i + 1,
            role="user" if i % 2 == 0 else "assistant",
            text=text,
        )
        for i in range(count)
    )


def test_deterministic_selection_excerpts_and_accounting():
    messages = history(33)
    assembler = ContextAssembler()
    result = assembler.assemble(messages[:-1], messages[-1], ())
    assert assembler.assemble(messages[:-1], messages[-1], ()) == result
    selected, memories, summary, policy = result
    assert [m.source_sequence for m in selected] == list(range(17, 34))
    assert summary.source_sequences == tuple(range(1, 17))
    assert summary.method == "bounded-excerpts-v1"
    assert policy.used == context_cost(selected, memories, summary)
    assert policy.used + policy.reserved <= policy.budget
    assert policy.omitted_messages == 0
    assert ExcerptSummarizer().summarize(()) is None


def test_unicode_budget_preserves_current_input_and_complete_turns():
    messages = history(33, "🌍" * 4000)
    selected, memories, summary, policy = ContextAssembler().assemble(
        messages[:-1], messages[-1], ()
    )
    assert len(selected) == 1
    assert selected[0].text == messages[-1].text
    assert summary is None
    assert policy.omitted_messages == 32
    assert policy.used == context_cost(selected, memories, summary)
    assert policy.used + policy.reserved <= policy.budget
    huge = messages[-1].model_copy(update={"text": "🌍" * 10000})
    with pytest.raises(ValidationError):
        ContextAssembler().assemble((), huge, ())


def test_memory_budget_and_excerpts_are_explicitly_bounded():
    messages = history(33, "x" * 500)
    candidates = tuple(
        ExplicitMemory(
            household_id=uuid4(), created_by=uuid4(), subject="Fact", content="🌍" * 1000
        )
        for _ in range(100)
    )
    selected, memories, summary, policy = ContextAssembler().assemble(
        messages[:-1], messages[-1], candidates
    )
    assert len(memories) < 100
    assert policy.used == context_cost(selected, memories, summary)
    assert policy.omitted_memories == 100 - len(memories)
    assert policy.used + policy.reserved <= policy.budget
    excerpts = ExcerptSummarizer().summarize(messages[:2])
    assert ("x" * 81) not in excerpts.text


def test_empty_history_and_legacy_snapshots():
    from jarvis.domain.conversations import Run

    current = history(1)[0]
    items, _, summary, policy = ContextAssembler().assemble((), current, ())
    assert len(items) == 1 and summary is None and policy.omitted_messages == 0
    old = Run(
        thread_id=current.thread_id,
        actor_id=uuid4(),
        context=(),
        input_message_id=current.id,
        output_message_id=uuid4(),
    ).model_dump(mode="json")
    for field in ("context_policy", "memory_context", "summary_context"):
        old.pop(field)
    assert Run.model_validate(old).context_policy is None


def test_active_memory_limit_and_retraction_free_capacity():
    from jarvis.adapters.memory import InMemoryStore
    from jarvis.domain.context import CreateMemory
    from jarvis.domain.models import ActorContext, Channel
    from jarvis.services.audit import AuditService
    from jarvis.services.memory import MemoryService

    store = InMemoryStore()
    service = MemoryService(store, AuditService(store))
    actor = ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"memories:read", "memories:write"}),
    )
    for i in range(100):
        store.insert_memory(
            ExplicitMemory(
                household_id=actor.household_id,
                created_by=actor.actor_id,
                subject=str(i),
                content="A fact",
            )
        )
    request = CreateMemory(subject="New", content="New fact", idempotency_key="capacity-memory-001")
    with pytest.raises(ValidationError, match="100 active"):
        service.create(actor, request)
    service.retract(actor, service.list(actor)[0].id)
    assert service.create(actor, request).accepted
    assert len(service.list(actor)) == 100
