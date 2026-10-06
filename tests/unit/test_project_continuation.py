from collections import Counter

import pytest

from simon.domain.agent_worker import WorkerResult
from simon.domain.errors import InvalidTransitionError, ValidationError
from simon.domain.models import JobStatus
from simon.domain.project_work import ConfigureProjectWork, ProjectAutonomy, ProjectWorkControl
from simon.services.agent_dispatcher import AgentDispatcher
from tests.unit.test_project_coordinator import harness, planning


def stopped_execution(tmp_path):
    h = harness(tmp_path)
    state = planning(h)
    state = h.work.control(
        h.actor,
        h.project.id,
        ProjectWorkControl(action="run_ready", expected_version=state.version),
    )
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    h.counts = Counter()

    class Worker:
        def execute(self, **kwargs):
            task = kwargs["task"]
            h.counts[task.id] += 1
            if task.id == "brief":
                return WorkerResult(
                    status="failed",
                    error_code="invalid_controller_response",
                    output="Useful partial brief",
                    steps=1,
                    input_tokens=10,
                    output_tokens=10,
                )
            return WorkerResult(
                status="succeeded",
                output="Retained verified research: use one local collection.",
                steps=1,
                input_tokens=10,
                output_tokens=10,
            )

    AgentDispatcher(h.runs, worker_factory=lambda *_: Worker()).tick()
    state = h.work.get(h.actor, h.project.id)
    update = h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert update
    h.work.apply_cycle(h.actor, h.project.id, update)
    h.failed = h.work.get(h.actor, h.project.id)
    return h


def test_continue_uses_exact_remaining_graph_and_never_repeats_completed_task(tmp_path):
    h = stopped_execution(tmp_path)
    old_run = h.runs.get(h.actor, h.failed.last_cycle.execution_run_id)
    assert old_run.status == JobStatus.FAILED
    assert h.coordinator.continuation_readiness(h.actor, h.project.id)["available"]
    continued = h.coordinator.continue_saved(
        h.actor,
        h.project.id,
        expected_version=h.failed.version,
        idempotency_key="continue-saved-once",
    )
    assert continued.active_cycle.parent_cycle_id == h.failed.last_cycle.id
    assert continued.active_cycle.phase == "ready" and continued.active_cycle.execution_approved
    assert continued.todos[0].status == "done" and continued.todos[0].run_id == old_run.id
    assert (
        h.coordinator.continue_saved(
            h.actor,
            h.project.id,
            expected_version=h.failed.version,
            idempotency_key="continue-saved-once",
        ).active_cycle.id
        == continued.active_cycle.id
    )
    plan_job = h.store.get_job(continued.active_cycle.execution_plan_id)
    specs = plan_job.input["request"]["tasks"]
    assert [spec["id"] for spec in specs] == ["brief", "lead-summary"]
    assert specs[0]["depends_on"] == []
    assert "Retained verified research" in specs[0]["additional_instructions"]
    assert "Useful partial brief" in specs[0]["additional_instructions"]
    assert "artifacts" in specs[0]["additional_instructions"]
    assert specs[1]["depends_on"] == ["brief"]

    class Worker:
        def execute(self, **kwargs):
            h.counts[kwargs["task"].id] += 1
            return WorkerResult(
                status="succeeded",
                output="Completed the retained launch brief",
                steps=1,
                input_tokens=10,
                output_tokens=10,
            )

    h.coordinator.advance(h.actor, h.project.id, continued.active_cycle)
    AgentDispatcher(h.runs, worker_factory=lambda *_: Worker()).tick()
    state = h.work.get(h.actor, h.project.id)
    h.work.apply_cycle(
        h.actor, h.project.id, h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    )
    assert h.work.get(h.actor, h.project.id).last_cycle.phase == "completed"
    assert h.counts == {"research": 1, "brief": 2, "lead-summary": 1}
    assert h.runs.get(h.actor, old_run.id) == old_run  # History was never rewritten.


@pytest.mark.parametrize("problem", ["unknown", "write", "unsettled_read", "missing_events"])
def test_continue_rejects_uncertain_or_previously_written_failed_assignment(tmp_path, problem):
    h = stopped_execution(tmp_path)
    run_id = h.failed.last_cycle.execution_run_id

    def change(run):
        task = next(task for task in run.tasks if task.id == "brief")
        updates = (
            {"status": "unknown"}
            if problem == "unknown"
            else {
                "tool_calls": 1,
                "events": ()
                if problem == "missing_events"
                else (
                    {
                        "event": "tool_dispatch",
                        "invocation_id": "one",
                        "side_effect": problem == "write",
                    },
                ),
            }
        )
        return run.model_copy(
            update={
                "tasks": tuple(
                    candidate.model_copy(update=updates) if candidate.id == task.id else candidate
                    for candidate in run.tasks
                )
            }
        )

    h.runs.update(run_id, change)
    assert not h.coordinator.continuation_readiness(h.actor, h.project.id)["available"]
    with pytest.raises(InvalidTransitionError):
        h.coordinator.continue_saved(
            h.actor,
            h.project.id,
            expected_version=h.failed.version,
            idempotency_key="unsafe-continuation",
        )
    assert h.work.get(h.actor, h.project.id).active_cycle is None


def test_bounded_policy_auto_approves_routine_plan_with_existing_finite_budget(tmp_path):
    h = harness(tmp_path)
    h.work.configure(
        h.actor,
        h.project.id,
        ConfigureProjectWork(
            expected_version=h.state.version,
            autonomy=ProjectAutonomy(execution_policy="bounded", model_budget_usd=1),
        ),
    )
    state = planning(h)
    assert state.active_cycle.bounded_execution
    assert state.active_cycle.execution_approved
    assert state.active_cycle.model_budget_usd == 1
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert h.work.get(h.actor, h.project.id).active_cycle.phase == "executing"


def test_scheduled_single_agent_cannot_delegate_to_other_members(tmp_path):
    h = harness(tmp_path)
    state = h.work.request_cycle(
        h.actor,
        h.project.id,
        "Draft a local brief",
        "single-agent-test",
        target_agent_id="writer",
        bounded_execution=True,
        model_budget_usd=1,
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    plan = h.platform.get(h.actor, state.active_cycle.planning_plan_id)
    assert plan.tasks[0].agent_id == "writer"
    from simon.domain.project_coordination import LeadDecision

    with pytest.raises(ValidationError, match="outside the project team"):
        h.coordinator._compile(h.actor, state, LeadDecision.model_validate(h.decision))


def test_actual_waiting_plan_creates_attention_record_without_pausing_project(tmp_path):
    h = harness(tmp_path)
    h.decision = {"status": "waiting", "summary": "Which launch date is approved?", "tasks": []}
    state = planning(h)
    assert state.last_cycle.phase == "waiting" and state.active_cycle is None
    assert not state.autonomy.paused and not state.blocked_reasons
    assert (
        h.coordinator.continuity.snapshot(h.actor, h.project.id)["waits"][0]["question"]
        == h.decision["summary"]
    )


@pytest.mark.parametrize(
    "identifier,policy,transport,allowed",
    [
        ("web.search", "read", "web_research", True),
        ("native.local_file_write", "write", "native", True),
        ("project.output_save", "write", "project_outputs", True),
        ("project.record_update", "write", "project_work", True),
        ("browser.click", "write", "browser", False),
        ("purchase", "external_commitment", "external", False),
        ("native.local_file_write", "external_commitment", "native", False),
        ("project.output_save", "write", "http_json", False),
    ],
)
def test_bounded_execution_never_auto_approves_external_or_generic_write_tools(
    tmp_path,
    monkeypatch,
    identifier,
    policy,
    transport,
    allowed,
):
    from simon.domain.tool_catalog import ToolDefinition

    h = harness(tmp_path)
    state = planning(h)
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    tool = ToolDefinition(
        id=identifier,
        description="Synthetic action",
        transport=transport,
        action_policy=policy,
        side_effect=policy != "read",
    )
    h.platform.manifest = h.platform.manifest.model_copy(update={"tools": (tool,)})
    plan = plan.model_copy(
        update={"tasks": (plan.tasks[0].model_copy(update={"tool_ids": (identifier,)}),)}
    )
    monkeypatch.setattr(h.platform, "get", lambda *_: plan)
    cycle = state.active_cycle.model_copy(update={"bounded_execution": True, "model_budget_usd": 1})
    assert h.coordinator.automatic_execution_allowed(h.actor, cycle) is allowed
    assert not h.coordinator.automatic_execution_allowed(
        h.actor, cycle.model_copy(update={"model_budget_usd": None})
    )
