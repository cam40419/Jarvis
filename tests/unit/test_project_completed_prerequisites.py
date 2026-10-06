import json
from uuid import uuid4

import pytest

from simon.domain.agent_platform import AgentTaskSpec
from simon.domain.agent_profiles import AgentRoleDefinition
from simon.domain.project_coordination import LeadDecision
from simon.domain.project_knowledge import PinnedProjectDecision, UpdateProjectKnowledge
from simon.domain.project_work import ConfigureProjectWork, ProjectTodo
from simon.services.agent_prompts import render_agent_prompt
from tests.unit.test_project_coordinator import harness
from tests.unit.test_project_knowledge_tools import fixture as knowledge_harness


def compile_saved_task(h, prerequisites, *, unrelated=()):
    work = h.work.request_cycle(
        h.actor,
        h.project.id,
        "Continue the verified research and save the assessment.",
        "saved-evidence-request",
    )
    pending = ProjectTodo(
        id="saved-assessment",
        title="Assess the research",
        objective="Assess the saved sources.",
        depends_on=tuple(item.id for item in prerequisites),
    )
    work = work.model_copy(update={"todos": (*prerequisites, pending, *unrelated)})
    with h.store.transaction(h.actor.workspace_id):
        work = h.work._save(h.work._job(h.actor, h.project.id), work)
    decision = LeadDecision.model_validate(
        {
            "status": "plan",
            "summary": "Use completed discovery without repeating it.",
            "tasks": [
                {
                    "id": "assess",
                    "title": "Assess",
                    "agent_id": "writer",
                    "objective": pending.objective,
                    "depends_on": [],
                    "tool_ids": [],
                    "todo_id": pending.id,
                }
            ],
        }
    )
    update = h.coordinator._compile(h.actor, work, decision)
    assert update.cycle.phase == "ready"
    job = h.store.get_job(update.cycle.execution_plan_id)
    specs = [AgentTaskSpec.model_validate(item) for item in job.input["request"]["tasks"]]
    return specs, update


def prerequisite(index=0, *, result="Verified source file_id=source-123; project_id=project-456."):
    return ProjectTodo(
        id=f"completed-research-{index}",
        title=f"Research {index}",
        objective="Discover source evidence",
        status="done",
        progress=100,
        result=result,
        run_id=uuid4(),
        plan_id=uuid4(),
        cycle_id=uuid4(),
        run_task_id=f"discover-{index}",
    )


def evidence(spec):
    return json.loads(spec.additional_instructions.split("must not be replayed): ", 1)[1])


def test_saved_completed_result_reaches_specialist_with_source_links_and_no_reexecution(tmp_path):
    h = harness(tmp_path)
    prior = prerequisite()
    specs, update = compile_saved_task(h, (prior,))
    assert [spec.id for spec in specs] == ["assess", "lead-summary"]
    assert specs[0].depends_on == () and specs[0].tool_ids == ()
    assert specs[1].depends_on == ("assess",)
    saved = evidence(specs[0])
    assert saved["omitted_count"] == 0
    assert saved["items"] == [
        {
            "todo_id": prior.id,
            "title": prior.title,
            "source_run_id": str(prior.run_id),
            "source_plan_id": str(prior.plan_id),
            "source_run_task_id": prior.run_task_id,
            "result": prior.result,
            "saved_result_chars": len(prior.result),
            "result_truncated": False,
        }
    ]
    writer = next(profile for profile in h.platform.manifest.agents if profile.id == "writer")
    rendered = render_agent_prompt(writer, specs[0], {})
    assert prior.result in rendered.prompt and str(prior.run_id) in rendered.prompt
    assert "exact file/document IDs and read context returned" in rendered.prompt
    assert "never invent identifiers" in rendered.prompt
    assert h.work.get(h.actor, h.project.id).todos[0] == prior
    assert update.todos[0].depends_on == (prior.id,)


def test_unrelated_done_tasks_are_not_included_as_specialist_evidence(tmp_path):
    h = harness(tmp_path)
    unrelated = prerequisite(99, result="Unrelated private result must not enter this task.")
    specs, _ = compile_saved_task(h, (), unrelated=(unrelated,))
    assert "Completed prerequisite evidence" not in specs[0].additional_instructions
    assert unrelated.result not in specs[0].additional_instructions


@pytest.mark.parametrize("fill", ["L", "\x01"])
def test_completed_evidence_is_bounded_with_maximum_role_knowledge_and_prerequisites(
    tmp_path, fill
):
    h = knowledge_harness(tmp_path)
    h.project.subject = fill * 200
    h.project.content = fill * 1000
    h.knowledge.update(
        h.actor,
        h.project.id,
        UpdateProjectKnowledge(
            expected_version=0,
            idempotency_key="maximum-saved-evidence",
            brief=fill * 8000,
            pinned_decisions=tuple(
                PinnedProjectDecision(
                    id=f"pin-{index}-" + "x" * 70,
                    title=f"Decision {index} " + fill * 140,
                    text=fill * 2000,
                )
                for index in range(20)
            ),
        ),
    )
    work = h.work.get(h.actor, h.project.id)
    team = work.team.model_copy(
        update={
            "members": {
                "writer": AgentRoleDefinition(
                    name="Research owner",
                    description="R" * 4000,
                    skill_ids=("tool.project.knowledge_read",),
                )
            }
        }
    )
    h.work.configure(
        h.actor, h.project.id, ConfigureProjectWork(expected_version=work.version, team=team)
    )
    prerequisites = tuple(prerequisite(index, result=fill * 16000) for index in range(100))
    specs, _ = compile_saved_task(h, prerequisites)
    for spec in specs:
        assert len(spec.additional_instructions) <= 16000
    saved = evidence(specs[0])
    assert 1 <= len(saved["items"]) <= 8
    assert len(saved["items"]) + saved["omitted_count"] == 100
    assert all(
        item["result_truncated"] and item["saved_result_chars"] == 16000 for item in saved["items"]
    )
    assert saved["items"][0]["source_run_id"] == str(prerequisites[0].run_id)
    assert saved["items"][0]["result"]
    assert all(
        pin.id in specs[0].additional_instructions
        for pin in h.knowledge.get(h.actor, h.project.id).pinned_decisions
    )


def test_manual_completed_prerequisite_has_honest_missing_run_provenance(tmp_path):
    h = harness(tmp_path)
    prior = ProjectTodo(
        id="manual-facts",
        title="Approved facts",
        objective="Review facts",
        status="done",
        result="User-entered findings.",
    )
    specs, _ = compile_saved_task(h, (prior,))
    item = evidence(specs[0])["items"][0]
    assert item["result"] == prior.result
    assert item["source_run_id"] is None and item["source_run_task_id"] is None
