import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters.project_work_tools import (
    ProjectWorkToolTransport,
    project_work_tool_definitions,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_profiles import AgentRoleDefinition
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.model_routing import TextGenerationResult
from simon.domain.models import JobStatus
from simon.domain.project_coordination import LeadDecision
from simon.domain.project_knowledge import PinnedProjectDecision, UpdateProjectKnowledge
from simon.domain.project_work import ConfigureProjectWork, ProjectActivityDraft
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import AgentWorker
from simon.services.project_coordinator import ProjectCoordinator
from tests.unit.test_project_coordinator import harness


def fixture(tmp_path):
    h = harness(tmp_path)
    manifest = h.platform.manifest.model_copy(
        update={
            "tools": project_work_tool_definitions(),
            "models": tuple(
                model.model_copy(update={"capabilities": frozenset({"text", "tools"})})
                for model in h.platform.manifest.models
            ),
            "agents": tuple(
                profile.model_copy(
                    update={
                        "tool_ids": ("project.knowledge_read", "project.history_search"),
                        "tool_scopes": frozenset({"jobs:read"}),
                        "max_steps": 5,
                    }
                )
                if profile.id == "lead"
                else profile
                for profile in h.platform.manifest.agents
            ),
        }
    )
    h.platform = AgentPlatformService(
        h.store,
        manifest,
        state_dir=tmp_path / "knowledge",
        environ={},
        available_transports=("project_work",),
    )
    h.runs = AgentRunService(h.platform, enabled=True, actor_resolver=lambda *_: h.actor)
    h.coordinator = ProjectCoordinator(h.work, h.runs)
    h.work.team_validator = h.coordinator.validate_team
    h.knowledge = h.coordinator.knowledge
    return h


def seed(h):
    h.work.record_activity(
        h.actor,
        h.project.id,
        ProjectActivityDraft(
            kind="finding",
            text="Archived linen research: supplier verified.",
            run_id=uuid4(),
        ),
        idempotency_key="seed-ancient-finding",
    )
    original = h.knowledge.history(h.actor, h.project.id, kind="finding").items[0]
    h.knowledge.update(
        h.actor,
        h.project.id,
        UpdateProjectKnowledge(
            expected_version=0,
            idempotency_key="seed-project-brief",
            brief="Build a durable clothing brand using natural fabrics.",
            pinned_decisions=(
                PinnedProjectDecision(
                    id="no-polyester",
                    title="Fabric policy",
                    text="No polyester in the pilot collection.",
                    source_activity_id=original.id,
                ),
            ),
        ),
    )
    for index in range(24):
        h.work.record_activity(
            h.actor,
            h.project.id,
            ProjectActivityDraft(
                kind="progress",
                text=f"Routine update {index}",
            ),
            idempotency_key=f"seed-recent-update-{index}",
        )
    return original


def start(h):
    state = h.work.request_cycle(h.actor, h.project.id, "Prepare linen launch.", "knowledge-cycle")
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    return h.work.get(h.actor, h.project.id)


def test_lead_retrieves_older_finding_through_real_dispatcher_and_context_reaches_workers(tmp_path):
    h = fixture(tmp_path)
    source = seed(h)
    state = start(h)
    assert state.active_cycle.phase == "planning"
    plan = h.platform.get(h.actor, state.active_cycle.planning_plan_id)
    assert set(plan.tasks[0].tool_ids) == {"project.knowledge_read", "project.history_search"}
    calls = []

    class Model:
        def generate(self, route, request):
            calls.append(request.prompt)
            if len(calls) == 1:
                assert "No polyester" in request.prompt
                assert "Archived linen research" in request.prompt
                response = {
                    "type": "tool",
                    "tool_id": "project.history_search",
                    "arguments": {"query": "Archived linen", "kind": "finding"},
                }
            elif len(calls) == 2:
                assert str(source.id) in request.prompt
                response = {"type": "tool", "tool_id": "project.knowledge_read", "arguments": {}}
            else:
                assert "Fabric policy" in request.prompt
                response = {"type": "final", "output": json.dumps(h.decision)}
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text=json.dumps(response),
                input_tokens=100,
                output_tokens=100,
            )

    def worker(profile, lease, actor, run_id):
        transports = TransportRegistry()
        transports.register(
            "project_work",
            ProjectWorkToolTransport(
                h.work,
                h.runs,
                actor=actor,
                run_id=run_id,
                revalidate=lambda: h.runs.live_actor(h.runs.job(run_id)),
            ),
        )
        return AgentWorker(Model(), h.platform.tools, transports)

    dispatcher = AgentDispatcher(h.runs, worker_factory=worker)
    completed = dispatcher.tick()
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.tasks[0].tool_calls == 2
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    saved = h.work.get(h.actor, h.project.id)
    assert saved.active_cycle.phase == "ready"
    execution = h.store.get_job(saved.active_cycle.execution_plan_id)
    for task in execution.input["request"]["tasks"]:
        assert "No polyester" in task["additional_instructions"]
        assert "durable clothing brand" in task["additional_instructions"]
    summary = execution.input["request"]["tasks"][-1]
    assert summary["objective"] == state.active_cycle.instruction
    assert "Answer the original project request directly" in summary["additional_instructions"]
    assert set(summary["tool_ids"]) == {"project.knowledge_read", "project.history_search"}


def test_read_tool_grants_are_not_added_to_tool_free_leads(tmp_path):
    h = harness(tmp_path)
    seed(SimpleNamespace(**h.__dict__, knowledge=h.coordinator.knowledge))
    state = start(h)
    plan = h.platform.get(h.actor, state.active_cycle.planning_plan_id)
    assert plan.tasks[0].tool_ids == ()


def test_tool_reads_are_run_project_bound_and_revalidate_current_permissions(tmp_path):
    h = fixture(tmp_path)
    seed(h)
    state = start(h)
    run_id = state.active_cycle.planning_run_id
    tools = {item.id: item for item in project_work_tool_definitions()}
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=run_id,
        agent_id="lead",
        scopes=h.actor.scopes,
        allowed_tool_ids=frozenset(tools),
    )
    current = [h.actor]
    transport = ProjectWorkToolTransport(
        h.work, h.runs, actor=h.actor, run_id=run_id, revalidate=lambda: current[0]
    )
    registry = TransportRegistry()
    registry.register("project_work", transport)

    def call(identifier, arguments=None, execution_context=context):
        return registry.execute(tools[identifier], arguments or {}, execution_context).output

    assert call("project.knowledge_read")["brief"].startswith("Build a durable")
    with pytest.raises(ToolCatalogError):
        call("project.history_search", {"project_id": str(uuid4())})
    with pytest.raises(NotFoundError):
        call("project.history_search", {"activity_id": str(uuid4())})
    with pytest.raises(AuthorizationError):
        call(
            "project.knowledge_read",
            execution_context=context.model_copy(
                update={"actor_id": uuid4()},
            ),
        )
    with pytest.raises(AuthorizationError):
        call("project.record_finding", {"text": "Cannot write from read context"})
    current[0] = h.actor.model_copy(update={"scopes": frozenset()})
    with pytest.raises(AuthorizationError):
        call("project.knowledge_read")


def test_long_history_entries_have_explicit_readable_continuations(tmp_path):
    h = fixture(tmp_path)
    text = "A" * 7000 + "SECRET-END"
    h.work.record_activity(
        h.actor,
        h.project.id,
        ProjectActivityDraft(kind="finding", text=text),
        idempotency_key="long-history-source",
    )
    source = h.knowledge.history(h.actor, h.project.id).items[0]
    state = start(h)
    run_id = state.active_cycle.planning_run_id
    transport = ProjectWorkToolTransport(
        h.work, h.runs, actor=h.actor, run_id=run_id, revalidate=lambda: h.actor
    )
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=run_id,
        agent_id="lead",
        scopes=h.actor.scopes,
        allowed_tool_ids=frozenset({"project.history_search"}),
    )
    definition = next(
        item for item in project_work_tool_definitions() if item.id == "project.history_search"
    )
    page = transport(definition, {}, context)
    preview = next(item for item in page["items"] if item["id"] == str(source.id))
    assert preview["text_truncated"] and len(preview["text"]) == 2000
    first = transport(definition, {"activity_id": str(source.id)}, context)
    second = transport(
        definition,
        {"activity_id": str(source.id), "text_offset": first["next_text_offset"]},
        context,
    )
    assert first["item"]["text"] + second["item"]["text"] == text
    assert second["next_text_offset"] is None


@pytest.mark.parametrize("fill", ["D", "\x01"])
def test_maximum_brief_pins_role_and_request_stay_within_task_contract(tmp_path, fill):
    h = fixture(tmp_path)
    h.knowledge.update(
        h.actor,
        h.project.id,
        UpdateProjectKnowledge(
            expected_version=0,
            idempotency_key="maximum-knowledge",
            brief="Brief: " + fill * 7993,
            pinned_decisions=tuple(
                PinnedProjectDecision(
                    id=f"pin-{index}",
                    title=f"Decision {index} " + fill * 140,
                    text="Approved: " + fill * 1990,
                )
                for index in range(20)
            ),
        ),
    )
    state = h.work.get(h.actor, h.project.id)
    team = state.team.model_copy(
        update={
            "members": {
                "writer": AgentRoleDefinition(
                    name="Research and documents",
                    description="Role: " + "R" * 3994,
                    skill_ids=("tool.project.knowledge_read",),
                )
            }
        }
    )
    h.work.configure(
        h.actor,
        h.project.id,
        ConfigureProjectWork(
            expected_version=state.version,
            team=team,
        ),
    )
    instruction = "Launch collection. " + "I" * (16000 - len("Launch collection. "))
    state = h.work.request_cycle(h.actor, h.project.id, instruction, "maximum-context-cycle")
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    assert state.active_cycle and state.active_cycle.phase == "planning", state.last_cycle
    planning = h.store.get_job(state.active_cycle.planning_plan_id)
    text = planning.input["request"]["tasks"][0]["additional_instructions"]
    assert len(text) <= 16000
    reference = json.loads(text.split("from this user data:\n", 1)[1])["context"]["knowledge"]
    assert reference["brief_truncated"] is True
    assert reference["pinned_decisions"][-1]["id"] == "pin-19"
    compiled = h.coordinator._compile(h.actor, state, LeadDecision.model_validate(h.decision))
    assert compiled.cycle.phase == "ready"
    execution = h.store.get_job(compiled.cycle.execution_plan_id)
    for task in execution.input["request"]["tasks"]:
        assert len(task["additional_instructions"]) <= 16000
        assert "Decision 19" in task["additional_instructions"]
    assert execution.input["request"]["tasks"][-1]["objective"] == instruction
