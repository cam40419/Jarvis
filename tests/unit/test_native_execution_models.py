"""Execution snapshots bound data and reject incoherent authority before persistence."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.adapters.memory import InMemoryStore
from simon.domain.native_execution import (
    EnrollExecutionRunner,
    NativeExecutionEvent,
    NativeExecutionPolicy,
    NativeExecutionRunner,
    NativeExecutionSchedule,
    NativeTaskWorkflow,
    RunnerLeaseCommand,
    UpdateExecutionPolicy,
    bounded_json,
)
from tests.contract.test_native_execution_store import run_record, seed_execution, step_record


@pytest.mark.parametrize(
    "changes",
    [
        {"max_active_runs": 0},
        {"max_queued_runs": 501},
        {"max_steps": 33},
        {"max_attempts": 9},
        {"max_depth": 5},
        {"max_children": 33},
        {"max_model_calls": 129},
        {"max_cost_microusd": -1},
        {"max_cost_microusd": True},
        {"lease_seconds": 10},
        {"run_timeout_seconds": 604801},
        {"expected_version": True},
        {"issued_by": str(uuid4())},
        {"workspace_id": str(uuid4())},
    ],
)
def test_execution_policy_command_does_not_accept_authority_or_unbounded_values(changes):
    with pytest.raises(ValidationError):
        UpdateExecutionPolicy(
            **{"expected_version": 0, "idempotency_key": "policy-command", **changes}
        )


def test_persisted_execution_policy_requires_issuer_and_implicit_defaults_deny_execution():
    policy = NativeExecutionPolicy(workspace_id=uuid4(), project_id=uuid4())
    assert not policy.enabled and not policy.auto_start and policy.max_cost_microusd == 0
    with pytest.raises(ValidationError):
        NativeExecutionPolicy.model_validate({**policy.model_dump(), "version": 1})


@pytest.mark.parametrize(
    "changes",
    [
        {"root_run_id": uuid4()},
        {"parent_run_id": uuid4()},
        {"depth": 1},
        {"schedule_id": uuid4()},
        {"schedule_definition_version": 1},
        {"policy_version": 2},
        {"issued_by": uuid4()},
        {"applied_step": 1},
        {"context": {"large": "x" * 131072}},
        {"context": {"bad": float("nan")}},
        {"input_digest": "not-a-digest"},
        {"engine": "anything"},
    ],
)
def test_execution_run_rejects_invalid_ancestry_grant_and_snapshots(changes):
    h = seed_execution(InMemoryStore())
    with pytest.raises(ValidationError):
        run_record(h, **changes)


def test_execution_deadline_and_utc_normalization():
    h = seed_execution(InMemoryStore())
    now = datetime(2026, 10, 8, tzinfo=timezone(timedelta(hours=5)))
    with pytest.raises(ValidationError):
        run_record(h, created_at=now, deadline_at=now)
    run = run_record(h, created_at=now, deadline_at=now + timedelta(hours=1))
    assert run.created_at.tzinfo == UTC and run.deadline_at.tzinfo == UTC
    with pytest.raises(ValidationError):
        run_record(h, deadline_at=datetime(2026, 10, 8))


def test_execution_json_is_detached_bounded_and_serializable():
    original = {"list": ["input"]}
    clone = bounded_json(original, 100)
    original["list"].append("later")
    assert clone == {"list": ["input"]}
    assert bounded_json({"literal": r"\u0000"}, 100) == {"literal": r"\u0000"}
    for value in ({"bad": object()}, {"bad": "\x00"}, {"bad": "\ud800"}, {"bad": float("inf")}):
        with pytest.raises(ValueError):
            bounded_json(value, 100)
    h = seed_execution(InMemoryStore())
    run = run_record(h)
    with pytest.raises(ValidationError):
        step_record(h, run, request={"prompt": "x" * 131072})
    with pytest.raises(ValidationError):
        step_record(h, run, result={"result": "x" * 32768})
    with pytest.raises(ValidationError):
        NativeExecutionEvent(
            workspace_id=h.workspace,
            project_id=h.first.id,
            run_id=run.id,
            sequence=1,
            kind="event",
            details={"result": "x" * 32768},
        )


def test_workflow_dependencies_are_distinct_and_never_self_referential():
    task = uuid4()
    for dependencies in ((task,), (uuid4(),) * 2, tuple(uuid4() for _ in range(33))):
        with pytest.raises(ValidationError):
            NativeTaskWorkflow(
                workspace_id=uuid4(), project_id=uuid4(), task_id=task, dependency_ids=dependencies
            )


def test_schedules_require_real_timezones_and_runner_tokens_do_not_serialize():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        NativeExecutionSchedule(
            workspace_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            issued_by=uuid4(),
            timezone="Imaginary/City",
        )
    with pytest.raises(ValidationError):
        NativeExecutionRunner(
            workspace_id=uuid4(),
            project_id=uuid4(),
            issued_by=uuid4(),
            name="Runner",
            token_hash="a" * 64,
            created_at=now,
            expires_at=now,
        )
    command = RunnerLeaseCommand(
        run_id=uuid4(), fence=1, lease_token="private-secret", idempotency_key="lease-action"
    )
    assert "private-secret" not in repr(command)
    assert "private-secret" not in command.model_dump_json()
    with pytest.raises(ValidationError) as error:
        RunnerLeaseCommand(
            run_id=uuid4(), fence=1, lease_token="secret" * 100, idempotency_key="lease-action"
        )
    assert "secretsecret" not in str(error.value)
    with pytest.raises(ValidationError):
        EnrollExecutionRunner(name="Runner", ttl_seconds=1, idempotency_key="enroll-action")
