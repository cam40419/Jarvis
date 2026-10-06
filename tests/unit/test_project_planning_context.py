"""Growing project history must not crowd current team grants out of planning."""

import json
from copy import deepcopy

import pytest

from simon.domain.errors import ValidationError
from simon.domain.project_work import ProjectTodo
from tests.unit.test_project_coordinator import harness


def test_backlog_references_distinguish_selectable_tasks_from_completed_or_held_work(tmp_path):
    h = harness(tmp_path)
    state = h.work.get(h.actor, h.project.id)
    statuses = ("todo", "ready", "running", "blocked", "unknown", "done", "cancelled")
    state = state.model_copy(
        update={
            "todos": tuple(
                ProjectTodo(
                    id="item-" + status, title=status, objective="Read sources", status=status
                )
                for status in statuses
            )
        }
    )
    backlog = h.coordinator._context(h.actor, state)["backlog"]
    assert {item["id"] for item in backlog if item["selectable"]} == {"item-todo", "item-ready"}
    assert {item["status"] for item in backlog} == set(statuses)


def test_large_history_compacts_without_losing_teammate_grants_or_pending_dependencies(
    tmp_path, monkeypatch
):
    h = harness(tmp_path)
    state = h.work.request_cycle(
        h.actor, h.project.id, "Assess and save the project", "large-history"
    )
    context = h.coordinator._context(h.actor, state)
    context["backlog"] = [
        {
            "id": f"task-{index}",
            "title": f"Deliverable {index}",
            "status": "ready" if index < 10 else "done",
            "agent_id": "writer",
            "depends_on": [f"task-{index - 1}"] if 0 < index < 10 else [],
            "objective": "Read and verify source evidence. " * 15,
            "result": "Previously saved report. " * 20,
        }
        for index in range(20)
    ]
    context["recent_activity"] = [{"kind": "progress", "text": "old work " * 60}] * 10
    context["past_findings"] = [
        {"id": str(index), "text": "finding " * 50, "text_truncated": True, "run_id": None}
        for index in range(6)
    ]
    roster = [
        {
            "agent_id": f"member-{index}",
            "name": f"Member {index}",
            "role": "A configurable team member. " * 20,
            "project_role": "Source research and writing. " * 15,
            "max_action": "write",
            "privacy": "cloud_allowed",
            "environments": [{"id": "workspace", "capabilities": ["python"], "network": "none"}],
            "ready_tools": [
                {
                    "id": f"project.tool_{tool}",
                    "description": "A source tool with explicit grants. " * 5,
                    "environment_capabilities": ["python"] if tool == 0 else [],
                }
                for tool in range(25)
            ],
        }
        for index in range(5)
    ]
    original = deepcopy(roster)
    monkeypatch.setattr(h.coordinator, "_context", lambda *_: deepcopy(context))
    text = h.coordinator._planning_prompt(h.actor, state, roster, max_chars=10500)
    assert len(text) <= 10500
    saved = json.loads(text.split("from this user data:\n", 1)[1])
    assert roster == original
    assert [[tool["id"] for tool in member["ready_tools"]] for member in saved["team"]] == [
        [tool["id"] for tool in member["ready_tools"]] for member in roster
    ]
    assert saved["team"][0]["ready_tools"][0]["environment_capabilities"] == ["python"]
    assert saved["team"][0]["environments"] == original[0]["environments"]
    pending = saved["context"]["backlog"]
    assert [item["id"] for item in pending] == [f"task-{i}" for i in range(10)]
    assert pending[-1]["depends_on"] == ["task-8"]
    assert saved["context"]["omitted_todos"] == 10
    assert saved["context"]["reference_compacted"] is True


def test_planning_reserves_space_for_controller_and_delegation_instructions(tmp_path, monkeypatch):
    h = harness(tmp_path)
    state = h.work.request_cycle(h.actor, h.project.id, "Assess project", "context-budget")
    seen = []

    def fill_budget(actor, current, roster, *, max_chars):
        seen.append(max_chars)
        return "R" * max_chars

    monkeypatch.setattr(h.coordinator, "_planning_prompt", fill_budget)
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    latest = h.work.get(h.actor, h.project.id)
    assert latest.active_cycle.phase == "planning"
    job = h.store.get_job(latest.active_cycle.planning_plan_id)
    assert len(job.input["request"]["tasks"][0]["additional_instructions"]) == 16000
    assert 0 < seen[0] < 16000


def test_capability_list_is_never_silently_truncated_to_fit(tmp_path):
    h = harness(tmp_path)
    state = h.work.request_cycle(h.actor, h.project.id, "Assess project", "roster-limit")
    roster = h.coordinator._planning_roster(h.actor, state)
    roster[0]["ready_tools"] = [
        {"id": "project.tool_" + str(index), "environment_capabilities": []} for index in range(500)
    ]
    with pytest.raises(ValidationError, match="team capability list"):
        h.coordinator._planning_prompt(h.actor, state, roster, max_chars=2500)


def test_context_limit_has_an_actionable_saved_error_without_creating_a_run(tmp_path, monkeypatch):
    h = harness(tmp_path)
    state = h.work.request_cycle(h.actor, h.project.id, "Assess project", "context-error")

    def oversized(*args, **kwargs):
        raise ValidationError("The team capability list exceeds the planning limit.")

    monkeypatch.setattr(h.coordinator, "_planning_prompt", oversized)
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    latest = h.work.get(h.actor, h.project.id)
    assert latest.active_cycle is None and latest.last_cycle.phase == "blocked"
    assert latest.last_cycle.error == "The team capability list exceeds the planning limit."
    assert latest.last_cycle.planning_run_id is None
    assert latest.last_cycle.planning_plan_id is None
