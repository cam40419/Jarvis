from datetime import timedelta
from uuid import uuid4

from simon.domain.connected_tools import ActionProposal, EmailDraft
from simon.domain.conversations import Run, Thread
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import utc_now


def test_recent_actions_uses_current_thread_and_completed_run_receipts(store):
    first = Thread(workspace_id=DEV_WORKSPACE_ID, created_by=DEV_ACTOR_ID, title="Office")
    other = Thread(workspace_id=DEV_WORKSPACE_ID, created_by=DEV_ACTOR_ID, title="Kitchen")
    started = utc_now()

    def record(thread: Thread, seconds: int) -> tuple[Run, ActionProposal]:
        run_id = uuid4()
        action = ActionProposal(
            workspace_id=DEV_WORKSPACE_ID,
            actor_id=DEV_ACTOR_ID,
            run_id=run_id,
            connection_id=uuid4(),
            kind="email.send",
            email=EmailDraft(to="test@example.com", subject="Test", body="Synthetic message"),
            created_at=started + timedelta(seconds=seconds),
        )
        run = Run(
            id=run_id,
            thread_id=thread.id,
            actor_id=DEV_ACTOR_ID,
            context=(),
            input_message_id=uuid4(),
            output_message_id=uuid4(),
            action_ids=(action.id,),
        )
        return run, action

    older_run, older = record(first, 0)
    newer_run, newer = record(first, 1)
    other_run, unrelated = record(other, 2)
    with store.transaction():
        store.insert_thread(first)
        store.insert_thread(other)
        for run, action in ((older_run, older), (newer_run, newer), (other_run, unrelated)):
            store.insert_run(run, ())
            store.save_action(action)
        unreferenced = newer.model_copy(update={"id": uuid4()})
        store.save_action(unreferenced)

    assert store.recent_actions(first.id, DEV_ACTOR_ID, 1) == (newer,)
    assert store.recent_actions(first.id, DEV_ACTOR_ID, 12) == (newer, older)
