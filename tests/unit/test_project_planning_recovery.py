"""Planning can use granted source reads and retry known pre-execution failures safely."""

import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from simon.adapters.memory import InMemoryStore
from simon.adapters.model_endpoints import ModelEndpointClient
from simon.adapters.native_tools import (
    native_tool_definitions,
    native_tool_status,
    native_transport_factory,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.domain.models import JobStatus
from simon.domain.project_files import ProjectFileCreate
from simon.domain.project_work import (
    ConfigureProjectWork,
    ProjectAutonomy,
    ProjectTeam,
    ProjectWorkControl,
)
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import AgentWorker
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_work import ProjectWorkService
from tests.contract.test_project_files import setup_project
from tests.unit.test_project_coordinator import harness, planning


def test_planning_lists_and_reads_actual_bound_project_files_with_fake_drive(tmp_path, monkeypatch):
    connected, actor, files, project_id = setup_project(InMemoryStore())
    nonce = "Approved source fact " + uuid4().hex
    saved = files.create_file(
        actor,
        ProjectFileCreate(project_id=project_id, name="brand.md", content=nonce),
        "seed-brand-notes",
        lambda: actor,
    )
    created_before = files.api.creates
    ids = {"native.project_files_list", "native.project_file_read", "native.project_file_create"}
    manifest = PlatformManifest(
        tools=tuple(tool for tool in native_tool_definitions(connected) if tool.id in ids),
        agents=(
            AgentProfile(
                id="lead",
                instructions="Assess the brand from authorized project files.",
                tool_ids=tuple(sorted(ids)),
                tool_scopes=actor.scopes,
                max_action="write",
                max_output_tokens=4096,
            ),
        ),
        teams=(TeamTemplate(id="brand", name="Brand", agent_ids=("lead",)),),
        models=(
            ModelEndpoint(
                id="model",
                provider="openai_compatible",
                model="test",
                base_url="http://localhost:1234/v1",
                local=True,
                capabilities=frozenset({"text", "tools"}),
            ),
        ),
    )
    platform = AgentPlatformService(
        connected.store,
        manifest,
        state_dir=tmp_path,
        environ={},
        available_transports=("native",),
        tool_availability=lambda current, identifier: native_tool_status(
            connected, current, identifier
        ),
    )
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: actor)
    work = ProjectWorkService(
        connected.store,
        project_resolver=connected.projects.project,
        actor_resolver=lambda *_: actor,
    )
    coordinator = ProjectCoordinator(work, runs)
    work.team_validator = coordinator.validate_team
    work.configure(
        actor,
        project_id,
        ConfigureProjectWork(
            expected_version=0,
            team=ProjectTeam(name="Brand", agent_ids=("lead",), lead_agent_id="lead"),
        ),
    )
    state = work.request_cycle(
        actor, project_id, "Look through the project files and assess the brand", "source-discovery"
    )
    coordinator.begin(actor, project_id, state.active_cycle)
    state = work.get(actor, project_id)
    plan = platform.get(actor, state.active_cycle.planning_plan_id)
    assert set(plan.tasks[0].tool_ids) == ids - {"native.project_file_create"}
    assert plan.tasks[0].environment is None
    calls = []

    def generate(self, route, request):
        calls.append(request.prompt)
        assert "Tool-controller response format:" in request.prompt
        assert "content of the final answer, not the outer response" in request.prompt
        assert "Return exactly one JSON object, no Markdown:" not in request.prompt
        if len(calls) == 1:
            response = {
                "type": "tool",
                "tool_id": "native.project_files_list",
                "arguments": {"project_id": str(project_id)},
            }
        elif len(calls) == 2:
            assert "brand.md" in request.prompt
            response = {
                "type": "tool",
                "tool_id": "native.project_file_read",
                "arguments": {"project_id": str(project_id), "file_id": saved["file_id"]},
            }
        else:
            assert nonce in request.prompt
            decision = {
                "status": "plan",
                "summary": "Assess the brand from the available, verified source documents.",
                "tasks": [
                    {
                        "id": "assess",
                        "title": "Brand assessment",
                        "agent_id": "lead",
                        "objective": "Assess the current brand based on " + nonce,
                        "tool_ids": [],
                        "depends_on": [],
                        "todo_id": None,
                    }
                ],
            }
            response = {"type": "final", "output": json.dumps(decision)}
        return TextGenerationResult(
            endpoint_id=route.endpoint_id,
            model=route.model,
            text=json.dumps(response),
            input_tokens=50,
            output_tokens=50,
        )

    monkeypatch.setattr(ModelEndpointClient, "generate", generate)
    dispatcher = AgentDispatcher(runs, transport_factory=native_transport_factory(connected))
    result = dispatcher.tick()
    assert result.status == JobStatus.SUCCEEDED and result.tasks[0].tool_calls == 2
    coordinator.advance(actor, project_id, state.active_cycle)
    state = work.get(actor, project_id)
    assert state.active_cycle.phase == "ready"
    assert nonce in state.todos[0].objective
    assert files.api.creates == created_before and files.api.edits == 0


@pytest.mark.parametrize("has_tools", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_rendered_planning_prompt_separates_controller_from_final_result(
    tmp_path, has_tools, structured
):
    h = harness(tmp_path)
    if has_tools:
        source_tools(h, tmp_path)
    if structured:
        manifest = h.platform.manifest.model_copy(
            update={
                "models": tuple(
                    model.model_copy(update={"provider": "openai_responses"})
                    for model in h.platform.manifest.models
                )
            }
        )
        h.platform = AgentPlatformService(
            h.store,
            manifest,
            state_dir=tmp_path / "structured",
            environ={},
            available_transports=("test",),
        )
        h.runs = AgentRunService(h.platform, enabled=True, actor_resolver=lambda *_: h.actor)
        h.coordinator = ProjectCoordinator(h.work, h.runs)
        h.work.team_validator = h.coordinator.validate_team
    state = h.work.request_cycle(
        h.actor, h.project.id, "Assess the current collection", "planning-response-format"
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    calls = []
    h.decision["summary"] = 'Assess the "Autumn" collection.\nCheck the café launch notes.'

    class Model:
        def generate(self, route, request):
            calls.append(request)
            assert "Return exactly one JSON object, no Markdown:" not in request.prompt
            # Follow the final format requested by the actual rendered prompt,
            # rather than silently wrapping every fixture response. The old
            # prompt requested the bare planning object even in controller mode.
            if request.response_schema is not None:
                assert "response schema is supplied, it takes precedence" in request.prompt
                response = {"action": {"type": "final", "output": h.decision, "artifacts": []}}
                Draft202012Validator(request.response_schema).validate(response)
            elif "serialize the complete planning result as a JSON string" in request.prompt:
                assert "bounded tool controller" in request.system
                assert "Do not return status, summary or tasks as top-level" in request.prompt
                response = {"type": "final", "output": json.dumps(h.decision)}
            else:
                assert "The lead has no tools for this planning turn" in request.prompt
                assert "tools are still available for delegated execution" in request.prompt
                assert "JSON object directly, with only status, summary and tasks" in request.prompt
                assert "bounded tool controller" not in request.system
                response = h.decision
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text=json.dumps(response),
                input_tokens=100,
                output_tokens=100,
            )

    transports = TransportRegistry()
    if has_tools:
        transports.register("test", lambda *_: pytest.fail("No tool call was requested"))
    dispatcher = AgentDispatcher(
        h.runs,
        worker_factory=lambda *_: AgentWorker(Model(), h.platform.tools, transports),
    )
    run = dispatcher.tick()
    assert run.status == JobStatus.SUCCEEDED and run.tasks[0].tool_calls == 0
    assert json.loads(run.tasks[0].output) == h.decision
    assert len(calls) == 1
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert h.work.get(h.actor, h.project.id).active_cycle.phase == "ready"


def source_tools(h, tmp_path, *, privacy="allow_cloud"):
    tools = (
        ToolDefinition(
            id="native.drive_search",
            description="Find source documents",
            transport="test",
            configured=True,
            required_scopes=frozenset({"jobs:read"}),
            settings={"network": True},
        ),
        ToolDefinition(
            id="native.local_file_read",
            description="Read a local file",
            transport="test",
            configured=True,
            required_scopes=frozenset({"jobs:read"}),
        ),
        ToolDefinition(
            id="native.local_file_write",
            description="Save a local file",
            transport="test",
            configured=True,
            side_effect=True,
            action_policy="write",
        ),
        ToolDefinition(
            id="cad.inspect",
            description="Inspect geometry in a workspace",
            transport="test",
            configured=True,
            environment_capabilities=frozenset({"cad"}),
        ),
        ToolDefinition(
            id="native.unavailable",
            description="A disconnected read",
            transport="test",
            configured=False,
        ),
        ToolDefinition(
            id="native.private",
            description="An ungranted scope",
            transport="test",
            configured=True,
            required_scopes=frozenset({"not:granted"}),
        ),
        ToolDefinition(
            id="native.other_agent",
            description="Not selected for the lead",
            transport="test",
            configured=True,
        ),
    )
    manifest = h.platform.manifest.model_copy(
        update={
            "tools": tools,
            "models": tuple(
                model.model_copy(update={"capabilities": frozenset({"text", "tools"})})
                for model in h.platform.manifest.models
            ),
            "agents": tuple(
                profile.model_copy(
                    update={
                        "tool_ids": tuple(tool.id for tool in tools[:-1]),
                        "tool_scopes": h.actor.scopes,
                        "privacy": privacy,
                        "max_action": "write",
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
        state_dir=tmp_path / "sources",
        environ={},
        available_transports=("test",),
    )
    h.runs = AgentRunService(h.platform, enabled=True, actor_resolver=lambda *_: h.actor)
    h.coordinator = ProjectCoordinator(h.work, h.runs)
    h.work.team_validator = h.coordinator.validate_team


def blocked(h):
    # An invalid plan blocks execution. A valid waiting decision instead creates
    # a durable question and leaves unrelated project work available.
    h.decision["tasks"][0]["agent_id"] = "unavailable-agent"
    state = planning(h)
    assert state.last_cycle.phase == "blocked" and state.autonomy.paused
    run = h.runs.get(h.actor, state.last_cycle.planning_run_id)
    assert run.tasks[0].error_code == "invalid_json_output"
    assert state.last_cycle.execution_plan_id is None
    return state


def test_lead_reads_granted_sources_before_planning_without_writing(tmp_path):
    h = harness(tmp_path)
    source_tools(h, tmp_path)
    state = h.work.request_cycle(
        h.actor, h.project.id, "Assess the source documents", "source-assessment"
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    plan = h.platform.get(h.actor, state.active_cycle.planning_plan_id)
    assert set(plan.tasks[0].tool_ids) == {"native.drive_search", "native.local_file_read"}
    assert plan.tasks[0].environment is None
    calls = []

    class Model:
        def generate(self, route, request):
            assert "An empty brief" in request.prompt
            assert "delegate discovery" in request.prompt
            if not calls:
                response = {"type": "tool", "tool_id": "native.drive_search", "arguments": {}}
            else:
                assert "Approved launch notes" in request.prompt
                response = {"type": "final", "output": json.dumps(h.decision)}
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text=json.dumps(response),
                input_tokens=50,
                output_tokens=50,
            )

    def handler(definition, arguments, context):
        calls.append(definition.id)
        assert (
            context.actor_id == h.actor.actor_id
            and context.run_id == state.active_cycle.planning_run_id
        )
        return {"files": [{"name": "Approved launch notes"}]}

    registry = TransportRegistry()
    registry.register("test", handler)
    dispatcher = AgentDispatcher(
        h.runs, worker_factory=lambda *_: AgentWorker(Model(), h.platform.tools, registry)
    )
    run = dispatcher.tick()
    assert run.status == JobStatus.SUCCEEDED
    assert calls == ["native.drive_search"]
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert h.work.get(h.actor, h.project.id).active_cycle.phase == "ready"


def test_local_only_lead_does_not_receive_cloud_planning_reads(tmp_path):
    h = harness(tmp_path)
    source_tools(h, tmp_path, privacy="local_only")
    state = h.work.get(h.actor, h.project.id)
    assert h.coordinator._lead_read_tools(h.actor, state, planning=True) == (
        "native.local_file_read",
    )


@pytest.mark.parametrize("status", ["waiting", "complete"])
def test_contradictory_lead_response_is_rejected_before_delegation(tmp_path, status):
    h = harness(tmp_path)
    h.decision["status"] = status
    h.decision["summary"] = "Discover the project documents with the updated access."
    state = planning(h)
    assert state.last_cycle.phase == "blocked"
    run = h.runs.get(h.actor, state.last_cycle.planning_run_id)
    assert run.tasks[0].error_code == "invalid_json_output"
    assert state.last_cycle.execution_plan_id is None and state.todos == ()
    assert h.coordinator.retry_readiness(h.actor, h.project.id)["can_retry"]


@pytest.mark.parametrize(
    "decision",
    [
        {"status": [], "summary": "Keep this rationale", "tasks": []},
        {"status": "plan", "summary": "Keep this rationale", "tasks": []},
        {
            "status": "waiting",
            "summary": "Keep this rationale",
            "tasks": [
                {
                    "id": "read",
                    "title": "Read sources",
                    "agent_id": "lead",
                    "objective": "Read sources",
                    "tool_ids": [],
                    "depends_on": [],
                    "todo_id": None,
                }
            ],
        },
    ],
)
def test_invalid_plan_shape_is_known_blocked_with_actionable_summary(tmp_path, decision):
    h = harness(tmp_path)
    state = h.work.request_cycle(
        h.actor, h.project.id, "Assess our collection", "legacy-invalid-plan"
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    # A historical response predates strict output schemas. The coordinator must
    # still explain it safely when reading old runs; new workers reject it sooner.
    h.runs.update(
        state.active_cycle.planning_run_id,
        lambda run: run.model_copy(
            update={
                "status": JobStatus.SUCCEEDED,
                "tasks": tuple(
                    task.model_copy(update={"status": "succeeded", "output": json.dumps(decision)})
                    for task in run.tasks
                ),
            }
        ),
    )
    update = h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    state = h.work.apply_cycle(h.actor, h.project.id, update)
    assert state.last_cycle.phase == "blocked"
    assert "Keep this rationale" in state.last_cycle.error
    assert "Retry planning" in state.last_cycle.error


def test_long_waiting_summary_preserves_durable_question_without_blocking_project(tmp_path):
    h = harness(tmp_path)
    h.decision = {"status": "waiting", "summary": "A" * 4000, "tasks": []}
    state = planning(h)
    assert state.last_cycle.phase == "waiting"
    assert len(state.last_cycle.error) == 2000
    assert state.active_cycle is None and not state.autonomy.paused
    assert state.blocked_reasons == ()
    wait = h.coordinator.continuity.snapshot(h.actor, h.project.id)["waits"][0]
    assert wait["question"] == state.last_cycle.error
    assert not h.coordinator.retry_readiness(h.actor, h.project.id)["can_retry"]


def test_retry_replans_original_request_with_current_grants_and_single_version_fence(tmp_path):
    h = harness(tmp_path)
    state = blocked(h)
    old_run = h.runs.get(h.actor, state.last_cycle.planning_run_id)
    source_tools(h, tmp_path)
    state = h.work.configure(
        h.actor,
        h.project.id,
        ConfigureProjectWork(
            expected_version=state.version,
            team=state.team.model_copy(
                update={"roles": {"lead": "Research the connected source files."}}
            ),
        ),
    )
    body = ProjectWorkControl(action="retry", expected_version=state.version)
    retry = h.work.control(h.actor, h.project.id, body)
    assert retry.active_cycle.instruction == "Prepare our clothing launch."
    assert retry.last_instruction == retry.active_cycle.instruction
    assert retry.last_cycle.id == state.last_cycle.id
    assert not retry.autonomy.paused and not retry.active_cycle.automatic
    assert retry.blocked_reasons == ()
    with pytest.raises(InvalidTransitionError):
        h.work.control(h.actor, h.project.id, body)
    h.coordinator.begin(h.actor, h.project.id, retry.active_cycle)
    current = h.work.get(h.actor, h.project.id)
    plan = h.platform.get(h.actor, current.active_cycle.planning_plan_id)
    assert "native.drive_search" in plan.tasks[0].tool_ids
    assert plan.team_version == retry.team.revision
    assert current.active_cycle.planning_run_id != old_run.id
    assert h.runs.get(h.actor, old_run.id) == old_run


def test_retry_disables_scheduled_continuation_but_keeps_its_limits(tmp_path):
    h = harness(tmp_path)
    state = blocked(h)
    state = h.work.configure(
        h.actor,
        h.project.id,
        ConfigureProjectWork(
            expected_version=state.version,
            autonomy=ProjectAutonomy(
                mode="scheduled",
                paused=True,
                objective="Assess our collection",
                cadence_minutes=15,
                max_cycles=3,
                model_budget_usd=2,
            ),
        ),
    )
    retry = h.work.control(
        h.actor, h.project.id, ProjectWorkControl(action="retry", expected_version=state.version)
    )
    assert retry.autonomy.mode == "manual" and not retry.autonomy.paused
    assert retry.autonomy.max_cycles == 3 and retry.autonomy.cadence_minutes == 15
    assert retry.active_cycle.model_budget_usd == 2 and retry.next_cycle_at is None


def test_legacy_continue_recovers_full_original_request_from_project_run_history(tmp_path):
    h = harness(tmp_path)
    state = blocked(h)
    job = h.work._job(h.actor, h.project.id)
    legacy = state.model_copy(
        update={
            "last_instruction": None,
            "last_cycle": state.last_cycle.model_copy(update={"instruction": "continue"}),
        }
    )
    h.work._save(job, legacy)
    state = h.work.get(h.actor, h.project.id)
    readiness = h.coordinator.retry_readiness(h.actor, h.project.id)
    assert readiness["instruction"] == "Prepare our clothing launch."
    retry = h.work.control(
        h.actor, h.project.id, ProjectWorkControl(action="retry", expected_version=state.version)
    )
    assert retry.active_cycle.instruction == readiness["instruction"]


@pytest.mark.parametrize("unsafe", ["unknown", "running", "executed"])
def test_retry_rejects_unknown_unsettled_and_previously_executed_work(tmp_path, unsafe):
    h = harness(tmp_path)
    state = blocked(h)
    if unsafe == "executed":
        state = h.work._save(
            h.work._job(h.actor, h.project.id),
            state.model_copy(
                update={
                    "last_cycle": state.last_cycle.model_copy(update={"execution_run_id": uuid4()})
                }
            ),
        )
    else:
        h.runs.update(
            state.last_cycle.planning_run_id,
            lambda run: run.model_copy(
                update={
                    "status": JobStatus.NEEDS_HUMAN if unsafe == "unknown" else JobStatus.RUNNING
                }
            ),
        )
    assert not h.coordinator.retry_readiness(h.actor, h.project.id)["can_retry"]
    with pytest.raises(InvalidTransitionError):
        h.work.control(
            h.actor,
            h.project.id,
            ProjectWorkControl(action="retry", expected_version=state.version),
        )
    assert h.work.get(h.actor, h.project.id).active_cycle is None


def test_retry_requires_current_owner_write_access(tmp_path):
    h = harness(tmp_path)
    state = blocked(h)
    body = ProjectWorkControl(action="retry", expected_version=state.version)
    with pytest.raises(AuthorizationError):
        h.work.control(
            h.actor.model_copy(update={"scopes": frozenset({"jobs:read"})}), h.project.id, body
        )
    with pytest.raises(NotFoundError):
        h.work.control(h.actor.model_copy(update={"actor_id": uuid4()}), h.project.id, body)
    assert h.work.get(h.actor, h.project.id).version == state.version


def test_continue_without_previous_request_asks_for_instruction(tmp_path):
    h = harness(tmp_path)
    with pytest.raises(ValidationError, match="no previous request"):
        h.work.request_cycle(h.actor, h.project.id, "continue", "no-original-request")
