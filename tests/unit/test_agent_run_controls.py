from uuid import uuid4, uuid5

import pytest

from simon.domain.errors import AuthorizationError, InvalidTransitionError
from simon.domain.execution import EnvironmentRequest
from simon.domain.models import JobStatus
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task


def test_claim_does_not_revive_cancellation_between_initial_read_and_locked_update(
    tmp_path,
    monkeypatch,
):
    harness = make_harness(tmp_path)
    queued = harness.queue((task("first"),))
    original = harness.runs.update
    intercepted = False

    def race(identifier, change, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            original(
                identifier,
                lambda state: state.model_copy(
                    update={
                        "status": JobStatus.CANCELLED,
                        "cancel_requested": True,
                    }
                ),
            )
        return original(identifier, change, **kwargs)

    monkeypatch.setattr(harness.runs, "update", race)
    assert harness.runs.claim(queued.id) is None
    saved = harness.runs.get(harness.actor, queued.id)
    assert saved.status == JobStatus.CANCELLED and saved.reserved_slots == 0
    assert not saved.execution_started


def test_failed_allocation_cleanup_survives_recording_failure(tmp_path, monkeypatch):
    harness = make_harness(tmp_path, environment=True)
    harness.backend.fail_allocation = True
    queued = harness.queue((task("first"),))
    original = harness.runs.task_update
    updates = 0

    def write_failure(*args, **kwargs):
        nonlocal updates
        updates += 1
        if updates == 2:  # First reserves the task; second records the recovered lease.
            raise RuntimeError("Database unavailable while recording lease")
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.runs, "task_update", write_failure)
    model = ControlledModel()
    completed = harness.dispatcher(model).execute(queued.id)
    assert completed.status == JobStatus.NEEDS_HUMAN
    assert len(harness.backend.released) == 1
    assert harness.backend.released[0].id == harness.backend.created[0].id
    assert model.calls == []


def test_operator_can_recover_revoked_owner_and_missing_lease_association(tmp_path):
    harness = make_harness(tmp_path, environment=True)
    queued = harness.queue((task("first"),))
    claimed = harness.runs.claim(queued.id)
    harness.runs.task_update(
        queued.id,
        "first",
        lambda record: record.model_copy(
            update={
                "status": "running",
            }
        ),
        executor_id=claimed.executor_id,
    )
    lease = harness.platform.environments.allocate(
        EnvironmentRequest(
            workspace_id=harness.actor.workspace_id,
            agent_id="worker",
            task_id=uuid4(),
            attempt_id=uuid5(queued.id, "first"),
        ),
        environment_id="shared",
    )

    def denied(*_args):
        raise AuthorizationError("Owner access revoked")

    harness.runs.actor_resolver = denied
    before = harness.runs.view(harness.runs.job(queued.id))
    recovered = harness.runs.recover_interrupted(
        queued.id,
        before.version,
        operator_actor_id=uuid4(),
    )
    assert recovered.status == JobStatus.NEEDS_HUMAN and recovered.reserved_slots == 0
    assert recovered.executor_id is None
    assert recovered.tasks[0].environment_lease_id == lease.id
    # Reconciliation does not claim the physical resource was safely stopped/released.
    assert harness.platform.environments.get(lease.id).status == "active"
    with pytest.raises(InvalidTransitionError):
        harness.runs.task_update(
            queued.id, "first", lambda record: record, executor_id=claimed.executor_id
        )
    harness.platform.environments.release(
        lease.id, attempt_id=lease.plan.request.attempt_id, fencing_token=lease.fencing_token
    )
