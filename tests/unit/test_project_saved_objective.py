"""An existing task's full authorized scope survives the lead's bounded planning view."""

import pytest

from simon.domain.errors import ValidationError
from simon.domain.project_coordination import LeadDecision
from simon.domain.project_work import ProjectTodo
from tests.unit.test_project_coordinator import harness


@pytest.mark.parametrize("length", [8000, 16000])
def test_selected_todo_preserves_full_original_objective_and_retained_source_context(
    tmp_path, length
):
    h = harness(tmp_path)
    prefix = (
        "Deliver a manufacturing report, numeric unit economics and an evidence-based comparison.\n"
    )
    suffix = "\nRETAINED_SOURCE_MARKER: exact supplier text and its source URL."
    original = prefix + "x" * (length - len(prefix) - len(suffix)) + suffix
    todo = ProjectTodo(
        id="saved-research", title="Saved research", objective=original, status="ready"
    )
    h.work.add_todo(h.actor, h.project.id, todo, idempotency_key="saved-objective")
    state = h.work.request_cycle(h.actor, h.project.id, "Finish the saved work.", "continue-saved")
    h.decision["tasks"][0].update(todo_id=todo.id, objective="Read a source and summarize it.")
    update = h.coordinator._compile(h.actor, state, LeadDecision.model_validate(h.decision))
    assert update.cycle.phase == "ready"
    job = h.store.get_job(update.cycle.execution_plan_id)
    specs = {task["id"]: task for task in job.input["request"]["tasks"]}
    selected = specs["research"]
    assert selected["objective"] == original
    assert selected["objective"].count("RETAINED_SOURCE_MARKER") == 1
    assert "RETAINED_SOURCE_MARKER" not in selected["additional_instructions"]
    assert specs["brief"]["objective"] == h.decision["tasks"][1]["objective"]
    assert update.todos[0].objective == original
    plan = h.platform.get(h.actor, update.cycle.execution_plan_id)
    assert plan.tasks[0].objective == original


def test_lead_rewrite_cannot_hide_saved_public_research_requirements(tmp_path):
    h = harness(tmp_path)
    todo = ProjectTodo(
        id="saved-public-research",
        title="Supplier assessment",
        objective="Find public sources and compare manufacturers for made-to-order clothing.",
        status="ready",
    )
    h.work.add_todo(h.actor, h.project.id, todo, idempotency_key="saved-public-objective")
    state = h.work.request_cycle(
        h.actor, h.project.id, "Finish the saved work.", "finish-saved-public"
    )
    h.decision["tasks"][0].update(todo_id=todo.id, objective="Write an outline.")
    with pytest.raises(ValidationError, match="requires public-web research"):
        h.coordinator._compile(h.actor, state, LeadDecision.model_validate(h.decision))
