import hashlib
import json
from uuid import uuid4

import pytest

from simon.domain.agent_runs import AgentRun, TaskExecution
from simon.domain.models import JobStatus
from simon.domain.project_work import ProjectCycle, ProjectWorkState
from simon.services.project_presentation import project_presentation


def scenario(phase="blocked", *, execution=False):
    workspace, actor, project, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    cycle = ProjectCycle(
        number=2,
        phase=phase,
        instruction="continue",
        planning_run_id=run_id,
        execution_run_id=run_id if execution else None,
    )
    state = ProjectWorkState(
        project_id=project,
        workspace_id=workspace,
        actor_id=actor,
        last_cycle=cycle,
    )
    run = AgentRun(
        id=run_id,
        plan_id=uuid4(),
        workspace_id=workspace,
        actor_id=actor,
        status=JobStatus.SUCCEEDED,
        tasks=(),
    )
    recovery = {
        "can_retry": not execution,
        "blocked_reasons": [],
        "instruction": "Review project files and assess the brand.",
    }
    return state, run, recovery


def test_succeeded_planning_call_never_masks_blocked_cycle_or_displays_raw_json():
    state, run, recovery = scenario()
    error = "The lead returned an invalid plan. Review its output."
    state = state.model_copy(
        update={
            "last_cycle": state.last_cycle.model_copy(
                update={"error": error},
            )
        }
    )
    output = json.dumps(
        {
            "status": "waiting",
            "summary": "I need the project files.",
            "tasks": [{"id": "inconsistent-plan"}],
        }
    )
    run = run.model_copy(
        update={
            "tasks": (
                TaskExecution(id="lead-plan", agent_id="lead", status="succeeded", output=output),
            )
        }
    )
    value = project_presentation(state, [run], blockers=[error], recovery=recovery)
    assert value["phase"] == "blocked" and value["status_label"] == "Needs attention"
    assert value["request"] == "Review project files and assess the brand."
    assert value["response"]["kind"] == "error"
    assert value["response"]["text"] == "I need the project files."
    assert value["response"]["phase"] == "planning"
    assert "delegated work did not start" in value["blockers"][0]


def test_completed_execution_prefers_lead_summary_over_task_or_older_planning_output():
    state, run, recovery = scenario("completed", execution=True)
    run = run.model_copy(
        update={
            "tasks": (
                TaskExecution(
                    id="lead-summary", agent_id="lead", status="succeeded", output="The answer"
                ),
                TaskExecution(
                    id="research", agent_id="researcher", status="succeeded", output="Notes"
                ),
            )
        }
    )
    old = run.model_copy(
        update={
            "id": uuid4(),
            "tasks": (TaskExecution(id="lead-plan", agent_id="lead", output="Old output"),),
        }
    )
    value = project_presentation(state, [run, old], blockers=[], recovery=recovery)
    assert value["response"]["kind"] == "answer"
    assert value["response"]["text"] == "The answer"
    assert value["response"]["run_id"] == str(run.id)


def test_partial_execution_does_not_claim_completion_and_keeps_unknown_outcome_visible():
    state, run, recovery = scenario("unknown", execution=True)
    run = run.model_copy(
        update={
            "status": JobStatus.NEEDS_HUMAN,
            "tasks": (
                TaskExecution(
                    id="research", agent_id="researcher", status="succeeded", output="Findings"
                ),
                TaskExecution(id="write", agent_id="writer", status="unknown"),
            ),
        }
    )
    value = project_presentation(
        state, [run], blockers=["Review the saved action"], recovery=recovery
    )
    assert value["response"]["kind"] == "partial"
    assert value["response"]["text"] == "Findings"
    assert value["status_label"] == "Outcome needs review"
    assert not value["recovery"]["can_retry"]


def test_missing_current_run_does_not_relabel_an_old_answer_as_current():
    state, run, recovery = scenario("planning")
    old = run.model_copy(
        update={
            "id": uuid4(),
            "tasks": (
                TaskExecution(
                    id="lead-summary", agent_id="lead", status="succeeded", output="Old answer"
                ),
            ),
        }
    )
    value = project_presentation(state, [old], blockers=[], recovery=recovery)
    assert value["response"] is None
    assert value["status_label"] == "Planning your request"


@pytest.mark.parametrize(
    "output",
    [
        "[" * 2000 + "0" + "]" * 2000,
        '{"status":"ready","tasks":[',
        '{"status":"ready","tasks":[]}',
        '```json\n{"status":"ready"',
    ],
)
def test_malformed_planning_output_gets_readable_explanation_without_breaking_project_view(output):
    state, run, recovery = scenario()
    run = run.model_copy(
        update={
            "tasks": (
                TaskExecution(id="lead-plan", agent_id="lead", status="succeeded", output=output),
            )
        }
    )
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    assert value["response"]["kind"] == "error"
    assert value["response"]["phase"] == "planning"
    assert "could not produce a readable plan" in value["response"]["text"]
    assert value["response"]["text"] != output


@pytest.mark.parametrize(
    ("code", "explanation"),
    [
        ("invalid_controller_response", "could not interpret as a tool action or final answer"),
        ("model_output_truncated", "reply was cut off"),
        ("model_refused", "provider declined"),
        ("worker_input_limit", "exceeded this agent's input limit"),
        ("invalid_tool_arguments", "did not match the tool's required format"),
        ("tool_not_authorized", "outside the access granted"),
        ("tool_unavailable", "selected tool was unavailable"),
        ("tool_execution_failed", "tool call failed"),
        ("incomplete_worker_output", "unfinished deliverable after one completion repair"),
        ("invalid_completion_review", "could not validate the completion review"),
        ("completion_review_unavailable", "available step or context limit"),
        ("future_error_code", "recorded error is available under technical details"),
    ],
)
def test_empty_failed_task_explains_recorded_failure_without_fabricating_an_answer(
    code, explanation
):
    state, run, recovery = scenario()
    run = run.model_copy(
        update={
            "status": JobStatus.FAILED,
            "tasks": (
                TaskExecution(
                    id="lead-plan", agent_id="lead", status="failed", error_code=code, steps=1
                ),
            ),
        }
    )
    old = run.model_copy(
        update={
            "id": uuid4(),
            "tasks": (
                TaskExecution(
                    id="lead-summary", agent_id="lead", status="succeeded", output="Old answer"
                ),
            ),
        }
    )
    value = project_presentation(
        state, [old, run], blockers=["Agent run stopped"], recovery=recovery
    )
    response = value["response"]
    assert response["kind"] == "error" and response["source"] == "system"
    assert response["title"] == "Planning could not finish"
    assert explanation in response["text"]
    assert "No answer was saved" in response["text"]
    assert "No tool calls were recorded" in response["text"]
    assert "Old answer" not in response["text"]
    assert response["run_id"] == str(run.id)
    assert response["failure"] == {"code": code, "status": "failed", "steps": 1, "tool_calls": 0}
    assert run.tasks[0].output == ""


def test_unknown_tool_outcome_never_claims_no_calls_or_suggests_retry():
    state, run, recovery = scenario("unknown", execution=True)
    run = run.model_copy(
        update={
            "status": JobStatus.NEEDS_HUMAN,
            "tasks": (
                TaskExecution(
                    id="create",
                    agent_id="lead",
                    status="unknown",
                    error_code="tool_execution_failed",
                    events=({"event": "tool_dispatch"},),
                ),
            ),
        }
    )
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    assert value["response"]["title"] == "Outcome needs review"
    assert "could not confirm" in value["response"]["text"]
    assert "No tool calls" not in value["response"]["text"]
    assert "Use Retry" not in value["response"]["text"]


def test_empty_execution_failure_selects_cause_before_blocked_dependent_task():
    state, run, recovery = scenario("blocked", execution=True)
    run = run.model_copy(
        update={
            "status": JobStatus.FAILED,
            "tasks": (
                TaskExecution(
                    id="lead-summary",
                    agent_id="lead",
                    status="blocked",
                    error_code="dependency_failed",
                ),
                TaskExecution(
                    id="research",
                    agent_id="researcher",
                    status="failed",
                    error_code="invalid_tool_arguments",
                    tool_calls=2,
                ),
            ),
        }
    )
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    assert value["response"]["task_id"] == "research"
    assert value["response"]["phase"] == "execution"
    assert "No tool calls" not in value["response"]["text"]
    assert "Use Retry" not in value["response"]["text"]


def test_new_queued_request_does_not_inherit_prior_run_failure():
    state, run, recovery = scenario("starting")
    previous = run.model_copy(
        update={
            "id": uuid4(),
            "tasks": (
                TaskExecution(
                    id="lead-plan",
                    agent_id="lead",
                    status="failed",
                    error_code="invalid_controller_response",
                ),
            ),
        }
    )
    run = run.model_copy(
        update={
            "status": JobStatus.QUEUED,
            "tasks": (TaskExecution(id="lead-plan", agent_id="lead"),),
        }
    )
    value = project_presentation(state, [previous, run], blockers=[], recovery=recovery)
    assert value["response"] is None


def completion_task(
    code="incomplete_worker_output", *, output="Partial comparison: Brand A uses editions."
):
    return TaskExecution(
        id="research_competition",
        agent_id="researcher",
        status="failed",
        error_code=code,
        output=output,
        steps=8,
        tool_calls=3,
        events=(
            {
                "event": "completion_review",
                "status": "partial",
                "summary": "The comparison covers Brand A, but Brand B has no source evidence.",
                "unmet_requirements": [
                    "Read Brand B's source material",
                    "Save the comparison document",
                ],
                "candidate_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            },
        ),
    )


@pytest.mark.parametrize(
    ("code", "explanation"),
    [
        ("incomplete_worker_output", "unfinished deliverable"),
        ("invalid_completion_review", "could not validate the completion review"),
        ("completion_review_unavailable", "available step or context limit"),
    ],
)
def test_completion_failure_keeps_partial_candidate_and_specific_reason_inline(code, explanation):
    state, run, recovery = scenario("blocked", execution=True)
    task = completion_task(code)
    run = run.model_copy(
        update={
            "status": JobStatus.FAILED,
            "tasks": (
                task,
                TaskExecution(
                    id="other-research", agent_id="other", status="succeeded", output="Other notes"
                ),
                TaskExecution(id="lead-summary", agent_id="lead", status="blocked"),
            ),
        }
    )
    value = project_presentation(state, [run], blockers=["Agent run stopped"], recovery=recovery)
    response = value["response"]
    assert response["kind"] == "partial"
    assert response["task_id"] == task.id
    assert response["title"].startswith("Partial response")
    assert explanation in response["text"]
    assert response["text"].endswith(task.output)
    assert "Brand B has no source evidence" in response["text"]
    assert "Read Brand B's source material" in response["text"]
    assert "Save the comparison document" in response["text"]
    assert "No answer was saved" not in response["text"]
    assert "Use Retry" not in response["text"]
    assert response["failure"] == {"code": code, "status": "failed", "steps": 8, "tool_calls": 3}
    assert any(explanation in reason for reason in value["blockers"])
    assert run.tasks[0].output == task.output


def test_stale_review_of_an_earlier_candidate_does_not_describe_a_repaired_partial():
    state, run, recovery = scenario("blocked", execution=True)
    task = completion_task("invalid_completion_review").model_copy(
        update={"output": "A newer partial comparison, with Brand B now included."}
    )
    run = run.model_copy(update={"status": JobStatus.FAILED, "tasks": (task,)})
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    assert "Brand B has no source evidence" not in value["response"]["text"]
    assert value["response"]["completion_review"] is None
    assert value["response"]["text"].endswith(task.output)


def test_review_details_are_bounded_and_malformed_saved_events_are_ignored():
    state, run, recovery = scenario("blocked", execution=True)
    task = completion_task()
    event = {
        **task.events[0],
        "summary": "x" * 5000,
        "unmet_requirements": ["r" * 2000, 12, None, " ", "Final requirement", "outside-limit"],
    }
    task = task.model_copy(update={"events": (event,)})
    run = run.model_copy(update={"status": JobStatus.FAILED, "tasks": (task,)})
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    review = value["response"]["completion_review"]
    assert len(review["summary"]) == 1000
    assert review["unmet_requirements"] == ["r" * 300, "Final requirement"]
    assert len(value["response"]["text"]) < 2000
    task = task.model_copy(
        update={"events": (*task.events, {"event": "completion_review", "status": []})}
    )
    run = run.model_copy(update={"tasks": (task,)})
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    assert value["response"]["completion_review"] is None


def test_new_cycle_does_not_inherit_old_completion_rejection_or_review():
    state, run, recovery = scenario("planning")
    old = run.model_copy(
        update={"id": uuid4(), "status": JobStatus.FAILED, "tasks": (completion_task(),)}
    )
    run = run.model_copy(update={"status": JobStatus.RUNNING, "tasks": ()})
    value = project_presentation(state, [old, run], blockers=[], recovery=recovery)
    assert value["response"] is None
    assert value["blockers"] == []


def test_failed_completion_candidate_is_never_presented_as_verified_answer():
    state, run, recovery = scenario("completed", execution=True)
    task = completion_task()
    run = run.model_copy(update={"status": JobStatus.FAILED, "tasks": (task,)})
    value = project_presentation(state, [run], blockers=[], recovery=recovery)
    assert value["response"]["kind"] == "partial"
    assert "Saved partial response" in value["response"]["text"]
