import json
from uuid import uuid4

import pytest

from simon.adapters.project_work_tools import (
    ProjectWorkToolTransport,
    project_work_tool_definitions,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import AgentTaskSpec, PlanTeamRequest
from simon.domain.agent_runs import StartAgentRun
from simon.domain.context import ExplicitMemory
from simon.domain.errors import AuthorizationError
from simon.domain.model_routing import TextGenerationResult
from simon.domain.models import JobStatus
from simon.domain.project_knowledge import PinnedProjectDecision, UpdateProjectKnowledge
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import AgentWorker
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_details import ProjectDetailsService
from tests.unit.test_project_coordinator import harness

TOOLS = (
    "project.details_read",
    "project.details_update",
    "project.knowledge_read",
    "project.knowledge_update",
)


def fixture(tmp_path, *, selected=TOOLS):
    h = harness(tmp_path)
    h.actor = h.actor.model_copy(
        update={"scopes": h.actor.scopes | {"memories:read", "memories:write"}}
    )
    h.project = ExplicitMemory(
        id=h.project.id,
        workspace_id=h.actor.workspace_id,
        created_by=h.actor.actor_id,
        subject=h.project.subject,
        content=h.project.content,
        scope="personal",
        category="project",
    )
    h.store.insert_memory(h.project)
    manifest = h.platform.manifest.model_copy(
        update={
            "tools": project_work_tool_definitions(),
            "models": tuple(
                model.model_copy(update={"capabilities": frozenset({"text", "tools"})})
                for model in h.platform.manifest.models
            ),
            "agents": tuple(
                agent.model_copy(
                    update={
                        "tool_ids": TOOLS,
                        "tool_scopes": h.actor.scopes,
                        "max_action": "write",
                        "max_steps": 8,
                    }
                )
                if agent.id == "lead"
                else agent
                for agent in h.platform.manifest.agents
            ),
        }
    )
    h.platform = AgentPlatformService(
        h.store,
        manifest,
        state_dir=tmp_path / "details",
        environ={},
        available_transports=("project_work",),
    )
    h.runs = AgentRunService(h.platform, enabled=True, actor_resolver=lambda *_: h.actor)
    h.coordinator = ProjectCoordinator(h.work, h.runs)
    h.work.team_validator = h.coordinator.validate_team
    h.work.actor_resolver = lambda *_: h.actor
    h.plan = h.platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id="project-" + h.project.id.hex,
            project_id=h.project.id,
            tasks=(
                AgentTaskSpec(
                    id="edit-project",
                    agent_id="lead",
                    objective="Update project details and saved context.",
                    tool_ids=selected,
                ),
            ),
            idempotency_key="details-tool-plan",
        ),
    )
    assert h.plan.state == "planned", h.plan.model_dump()
    h.run = h.runs.start(h.actor, h.plan.id, StartAgentRun(idempotency_key="details-tool-run"))
    h.current = [h.actor]
    h.transport = ProjectWorkToolTransport(
        h.work, h.runs, actor=h.actor, run_id=h.run.id, revalidate=lambda: h.current[0]
    )
    h.registry = TransportRegistry()
    h.registry.register("project_work", h.transport)
    h.context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=h.run.id,
        agent_id="lead",
        scopes=h.actor.scopes,
        allowed_tool_ids=frozenset(selected),
        authorized_action="write",
    )
    h.tools = {item.id: item for item in project_work_tool_definitions()}
    return h


def call(h, name, arguments=None, *, context=None):
    return h.registry.execute(h.tools[name], arguments or {}, context or h.context).output


def test_project_agent_edits_persist_through_real_dispatcher_without_provider_calls(tmp_path):
    h = fixture(tmp_path)
    pinned = PinnedProjectDecision(id="fabric", title="Fabric", text="Use linen.")
    h.coordinator.knowledge.update(
        h.actor,
        h.project.id,
        UpdateProjectKnowledge(
            expected_version=0, idempotency_key="seed-project-decision", pinned_decisions=(pinned,)
        ),
    )
    responses = iter(
        [
            {"type": "tool", "tool_id": "project.details_read", "arguments": {}},
            {
                "type": "tool",
                "tool_id": "project.details_update",
                "arguments": {
                    "expected_version": 0,
                    "name": "Linen studio",
                    "description": "A durable everyday collection.",
                },
            },
            {"type": "tool", "tool_id": "project.knowledge_read", "arguments": {}},
            {
                "type": "tool",
                "tool_id": "project.knowledge_update",
                "arguments": {
                    "expected_version": 1,
                    "brief": "Supplier research verified; start with linen samples.",
                },
            },
            {
                "type": "final",
                "output": (
                    "Updated the project name, description and brief; retained the fabric decision."
                ),
            },
        ]
    )

    class Model:
        def generate(self, route, request):
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text=json.dumps(next(responses)),
                input_tokens=30,
                output_tokens=30,
            )

    dispatcher = AgentDispatcher(
        h.runs, worker_factory=lambda *_: AgentWorker(Model(), h.platform.tools, h.registry)
    )
    finished = dispatcher.tick()
    assert finished.status == JobStatus.SUCCEEDED
    assert finished.tasks[0].tool_calls == 4
    completions = [item for item in finished.tasks[0].events if item["event"] == "tool_complete"]
    assert len(completions) == 4 and all(item["status"] == "succeeded" for item in completions)
    assert ProjectDetailsService(h.work).get(h.actor, h.project.id).name == "Linen studio"
    knowledge = h.coordinator.knowledge.get(h.actor, h.project.id)
    assert knowledge.brief.startswith("Supplier research") and knowledge.pinned_decisions == (
        pinned,
    )
    activity = h.work.list_activity(h.actor, h.project.id)["items"]
    assert sum(item["run_id"] == str(h.run.id) for item in activity) == 2


def test_invocation_replay_conflict_and_expected_version(tmp_path):
    h = fixture(tmp_path)
    args = {"expected_version": 0, "name": "Saved exactly once"}
    saved = call(h, "project.details_update", args)
    assert call(h, "project.details_update", args) == saved
    with pytest.raises(ToolExecutionError) as conflict:
        call(h, "project.details_update", {**args, "name": "Changed request"})
    assert not conflict.value.unknown
    with pytest.raises(ToolExecutionError) as stale:
        call(
            h,
            "project.details_update",
            args,
            context=h.context.model_copy(update={"invocation_id": uuid4()}),
        )
    assert not stale.value.unknown
    assert saved["version"] == 1


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("project.details_update", {"expected_version": 0, "name": "No"}),
        ("project.knowledge_update", {"expected_version": 0, "brief": "No"}),
    ],
)
def test_write_tools_require_action_grant_and_live_scopes(tmp_path, name, arguments):
    h = fixture(tmp_path)
    for context in (
        h.context.model_copy(update={"authorized_action": "read"}),
        h.context.model_copy(update={"allowed_tool_ids": frozenset()}),
        h.context.model_copy(update={"actor_id": uuid4()}),
        h.context.model_copy(update={"workspace_id": uuid4()}),
        h.context.model_copy(update={"run_id": uuid4()}),
        h.context.model_copy(update={"agent_id": "unassigned"}),
    ):
        with pytest.raises(AuthorizationError):
            call(h, name, arguments, context=context)
    h.current[0] = h.actor.model_copy(update={"scopes": h.actor.scopes - {"jobs:write"}})
    with pytest.raises(AuthorizationError):
        call(h, name, arguments)
    assert ProjectDetailsService(h.work).get(h.actor, h.project.id).version == 0
    assert h.coordinator.knowledge.get(h.actor, h.project.id).version == 0


def test_project_identity_is_server_bound_and_new_plan_is_required_for_write_grant(tmp_path):
    h = fixture(tmp_path, selected=("project.details_read",))
    with pytest.raises(ToolCatalogError):
        call(h, "project.details_read", {"project_id": str(uuid4())})
    forged = h.context.model_copy(update={"allowed_tool_ids": frozenset(TOOLS)})
    with pytest.raises(AuthorizationError, match="saved plan"):
        call(h, "project.details_update", {"expected_version": 0, "name": "No"}, context=forged)
    definition = h.tools["project.details_update"].model_copy(
        update={"side_effect": False, "action_policy": "read", "required_scopes": frozenset()}
    )
    with pytest.raises(ToolCatalogError):
        h.transport(definition, {"expected_version": 0, "name": "No"}, forged)


@pytest.mark.parametrize(
    "tool_id,arguments",
    [
        ("project.details_update", {"expected_version": 99, "name": "Stale edit"}),
        ("project.knowledge_update", {"expected_version": 99, "brief": "Stale edit"}),
        ("project.details_update", {"expected_version": 0}),
        ("project.knowledge_update", {"expected_version": 0}),
    ],
)
def test_known_project_edit_rejection_stops_worker_without_uncertainty(
    tmp_path, tool_id, arguments
):
    h = fixture(tmp_path)
    calls = []

    class Model:
        def generate(self, route, request):
            calls.append(request)
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text=json.dumps({"type": "tool", "tool_id": tool_id, "arguments": arguments}),
                input_tokens=30,
                output_tokens=30,
            )

    dispatcher = AgentDispatcher(
        h.runs, worker_factory=lambda *_: AgentWorker(Model(), h.platform.tools, h.registry)
    )
    result = dispatcher.tick()
    assert (
        result.status == JobStatus.FAILED and result.tasks[0].error_code == "tool_execution_failed"
    )
    assert len(calls) == 1 and result.tasks[0].tool_calls == 1
    assert ProjectDetailsService(h.work).get(h.actor, h.project.id).version == 0
    assert h.coordinator.knowledge.get(h.actor, h.project.id).version == 0


def test_unexpected_project_storage_failure_remains_uncertain(tmp_path, monkeypatch):
    h = fixture(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("private database detail")

    monkeypatch.setattr(ProjectDetailsService, "update", fail)
    with pytest.raises(RuntimeError, match="private database"):
        call(h, "project.details_update", {"expected_version": 0, "name": "No"})
