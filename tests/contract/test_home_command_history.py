from uuid import uuid4

from simon.domain.conversations import Thread
from simon.domain.home import HomeChange, HomeCommand
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID


def test_thread_command_history_is_filtered_before_limit(store):
    first = Thread(
        household_id=DEV_HOUSEHOLD_ID, created_by=DEV_ACTOR_ID, title="First room"
    )
    second = Thread(
        household_id=DEV_HOUSEHOLD_ID, created_by=DEV_ACTOR_ID, title="Second room"
    )
    with store.transaction():
        store.insert_thread(first)
        store.insert_thread(second)
        original = HomeCommand(
            id=uuid4(),
            household_id=DEV_HOUSEHOLD_ID,
            actor_id=DEV_ACTOR_ID,
            thread_id=first.id,
            run_id=uuid4(),
            device_name="Desk lamp",
            change=HomeChange(device_id="desk-lamp", on=False),
        )
        store.save_home_command(original)
        for number in range(101):
            store.save_home_command(
                HomeCommand(
                    id=uuid4(),
                    household_id=DEV_HOUSEHOLD_ID,
                    actor_id=DEV_ACTOR_ID,
                    thread_id=second.id,
                    run_id=uuid4(),
                    device_name=f"Other {number}",
                    change=HomeChange(device_id="other-lamp", on=False),
                )
            )
    assert len(store.home_commands(DEV_HOUSEHOLD_ID, DEV_ACTOR_ID)) == 100
    assert store.home_commands(
        DEV_HOUSEHOLD_ID, DEV_ACTOR_ID, thread_id=first.id, limit=32
    ) == (original,)
