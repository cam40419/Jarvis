from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from simon.domain.conversations import Message, Run, Thread
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.ports import Store


def thread(store: Store, title: str = "Ordered conversation") -> Thread:
    record = Thread(workspace_id=DEV_WORKSPACE_ID, created_by=DEV_ACTOR_ID, title=title)
    store.insert_thread(record)
    return record


def answer(
    store: Store,
    conversation: Thread,
    identifier: UUID,
    created_at: datetime,
    sequence: int | None = None,
    output_id: UUID | None = None,
) -> Run:
    input_id = uuid4()
    output_id = output_id or uuid4()
    if sequence is not None:
        store.insert_message(
            Message(
                id=input_id,
                thread_id=conversation.id,
                sequence=sequence - 1,
                role="user",
                text="Question",
                created_at=created_at,
            )
        )
        store.insert_message(
            Message(
                id=output_id,
                thread_id=conversation.id,
                sequence=sequence,
                role="assistant",
                text="Answer",
                created_at=created_at,
            )
        )
    run = Run(
        id=identifier,
        thread_id=conversation.id,
        actor_id=DEV_ACTOR_ID,
        context=(),
        input_message_id=input_id,
        output_message_id=output_id,
        created_at=created_at,
        completed_at=created_at,
    )
    store.insert_run(run, ())
    return run


def test_equal_timestamps_and_reverse_uuids_follow_answer_message_sequence(store: Store) -> None:
    conversation = thread(store)
    stamp = datetime(2026, 9, 30, 12, tzinfo=UTC)
    # UUID order and insertion order both deliberately oppose conversation order.
    third = answer(store, conversation, UUID(int=1), stamp, sequence=6)
    second = answer(store, conversation, UUID(int=2), stamp, sequence=4)
    first = answer(store, conversation, UUID(int=3), stamp, sequence=2)
    assert store.answer_runs(conversation.id, 0, 10) == (first, second, third)
    assert store.latest_run(conversation.id) == third
    assert tuple(store.answer_runs(conversation.id, index, 1)[0] for index in range(3)) == (
        first,
        second,
        third,
    )
    assert store.answer_runs(conversation.id, 3, 1) == ()


def test_legacy_runs_without_messages_remain_ordered_and_do_not_replace_latest_answer(
    store: Store,
) -> None:
    conversation = thread(store)
    stamp = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert store.latest_run(conversation.id) is None
    assert store.answer_runs(conversation.id, 0, 10) == ()
    older = answer(store, conversation, UUID(int=9), stamp)
    newer = answer(store, conversation, UUID(int=8), stamp + timedelta(seconds=1))
    assert store.answer_runs(conversation.id, 0, 10) == (older, newer)
    assert store.latest_run(conversation.id) == newer
    # Clock ordering is deliberately wrong; the actual answer sequence takes priority.
    completed = answer(store, conversation, UUID(int=7), stamp - timedelta(days=1), sequence=2)
    assert store.answer_runs(conversation.id, 0, 10) == (older, newer, completed)
    assert store.latest_run(conversation.id) == completed


def test_message_reference_in_other_thread_cannot_influence_answer_order(store: Store) -> None:
    conversation = thread(store)
    other = thread(store, "Another conversation")
    stamp = datetime(2026, 9, 30, 12, tzinfo=UTC)
    actual = answer(store, conversation, UUID(int=1), stamp, sequence=2)
    unrelated = answer(store, other, UUID(int=2), stamp, sequence=100)
    missing = answer(
        store,
        conversation,
        UUID(int=3),
        stamp + timedelta(days=1),
        output_id=unrelated.output_message_id,
    )
    assert store.answer_runs(conversation.id, 0, 10) == (missing, actual)
    assert store.latest_run(conversation.id) == actual
    assert store.answer_runs(other.id, 0, 10) == (unrelated,)
