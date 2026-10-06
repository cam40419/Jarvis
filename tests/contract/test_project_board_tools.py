"""ClickUp skills use scoped runs and the existing durable board outbox, never live providers."""

import json
from uuid import uuid4

import pytest

from simon.adapters.project_board_tools import (
    ProjectBoardToolTransport,
    project_board_tool_definitions,
    project_board_tool_status,
    project_board_transport_factory,
)
from simon.adapters.tool_preflight import integration_status, uses_network
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import AuthorizationError
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import JobStatus
from simon.domain.project_board_state import BindProjectBoard
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from tests.contract.test_project_boards import add, publish, runtime
from tests.contract.test_project_boards import board as board
from tests.contract.test_project_history import saved_run
from tests.contract.test_project_work import project_work as project_work
from tests.unit.test_agent_dispatcher import ControlledModel


@pytest.fixture
def tools(board, tmp_path):
    h = board
    h.actor = h.actor.model_copy(update={"scopes": h.actor.scopes | {"memories:read"}})
    definitions = project_board_tool_definitions()
    team = TeamTemplate(id="studio", name="Board tools", agent_ids=("writer",))
    h.platform = AgentPlatformService(
        h.store,
        PlatformManifest(
            agents=(
                AgentProfile(
                    id="writer",
                    instructions="Manage the explicitly assigned project board.",
                    tool_ids=tuple(tool.id for tool in definitions),
                    tool_scopes=h.actor.scopes,
                    max_action="write",
                ),
            ),
            teams=(team,),
            tools=definitions,
            models=(
                ModelEndpoint(
                    id="synthetic",
                    provider="openai_compatible",
                    model="synthetic",
                    local=True,
                    base_url="http://127.0.0.1:11434/v1",
                    capabilities=frozenset({"text", "tools"}),
                ),
            ),
        ),
        state_dir=tmp_path,
        environ={},
        available_transports=("project_boards",),
        tool_availability=lambda actor, key: project_board_tool_status(h.bridge, actor, key),
    )
    h.platform.project_visibility_resolver = h.work.project_resolver
    h.platform.project_team_resolver = lambda *_: team
    h.platform.project_tool_availability = lambda actor, project, key: project_board_tool_status(
        h.bridge,
        actor,
        key,
        project,
    )
    h.runs = AgentRunService(h.platform, enabled=True, actor_resolver=lambda *_: h.actor)
    h.run = saved_run(h.store, h.project_id, actor=h.actor)
    job = h.store.get_job(h.run.id)
    state = h.run.model_copy(
        update={
            "status": JobStatus.RUNNING,
            "tasks": (h.run.tasks[0].model_copy(update={"status": "running"}),),
        }
    )
    h.store.save_job(
        job.model_copy(
            update={"status": JobStatus.RUNNING, "result": state.model_dump(mode="json")}
        ),
        job.version,
    )
    h.definitions = {tool.id: tool for tool in definitions}
    h.context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=h.run.id,
        agent_id="writer",
        scopes=h.actor.scopes,
        allowed_tool_ids=frozenset(h.definitions),
        authorized_action="write",
    )
    h.handler = ProjectBoardToolTransport(
        h.bridge, h.runs, actor=h.actor, run_id=h.run.id, revalidate=lambda: h.actor
    )
    return h


def call(h, identifier, values=None, *, context=None):
    return h.handler(h.definitions["clickup." + identifier], values or {}, context or h.context)


def test_individual_read_skills_are_bounded_and_project_scoped(tools):
    h = tools
    for index in range(15):
        h.adapter.task(f"item{index}", description="x" * 10000)
    page = call(h, "tasks_list", {"limit": 5})
    assert len(page["tasks"]) == 5 and page["next_offset"] == 5
    assert len(page["tasks"][0]["description_excerpt"]) == 240
    assert page["untrusted_source"] and len(json.dumps(page)) < 6000
    task = call(h, "task_read", {"task_id": "item0", "offset": 12, "limit": 100})
    assert task["description"] == "x" * 100 and task["next_offset"] == 112
    h.adapter.change("item0", list_id="999")
    with pytest.raises(AuthorizationError):
        call(h, "task_read", {"task_id": "item0"})
    h.adapter.calls.clear()
    for changes in (
        {"run_id": uuid4()},
        {"agent_id": "other"},
        {"actor_id": uuid4()},
        {"scopes": h.actor.scopes - {"memories:read"}},
    ):
        with pytest.raises(AuthorizationError):
            call(h, "tasks_list", context=h.context.model_copy(update=changes))
    assert h.adapter.calls == []


def test_unconfigured_remote_skills_stay_visible_with_offline_snapshot(tools):
    h = tools
    h.bridge.configured_connections = ()
    h.adapter.calls.clear()
    catalog = h.platform.catalog(h.actor)
    statuses = {row["id"]: row for row in catalog["tool_statuses"]}
    assert statuses["clickup.project_read"]["state"] == "configured"
    assert statuses["clickup.tasks_publish"]["state"] == "unavailable"
    assert "Connect your ClickUp account in Connections" in str(
        statuses["clickup.tasks_publish"]["blocked_reasons"],
    )
    assert "tool.clickup.tasks_publish" in {row["id"] for row in catalog["individual_skills"]}
    offline = call(h, "project_read")
    assert offline["binding"]["list_id"] == "456" and not offline["connection_available"]
    assert call(h, "tasks_list")["requires_setup"]
    assert h.adapter.calls == []


def test_publish_is_durable_active_cycle_safe_and_unknown_never_replayed(tools):
    h = tools
    add(h)
    h.work.request_cycle(h.actor, h.project_id, "Work on the project", "board-tools-cycle")
    request = {"todo_ids": ["research"], "expected_version": h.bound.version}
    result = call(h, "tasks_publish", request)
    assert result["status"] == "succeeded"
    assert len(result["project"]["mappings"]) == 1
    assert call(h, "tasks_publish", request)["status"] == "succeeded"
    assert len([row for row in h.adapter.calls if row[0] == "create"]) == 1
    for operation in ("tasks_import", "sync"):
        body = {"expected_version": h.bridge.get(h.actor, h.project_id).version}
        if operation == "tasks_import":
            body["task_ids"] = [result["project"]["mappings"][0]["remote_id"]]
        blocked = call(h, operation, body)
        assert blocked["status"] == "blocked" and "settle" in blocked["reason"]
    add(h, "second")
    h.adapter.fail_create = "unknown"
    body = {"todo_ids": ["second"], "expected_version": h.bridge.get(h.actor, h.project_id).version}
    with pytest.raises(ToolExecutionError) as error:
        call(
            h,
            "tasks_publish",
            body,
            context=h.context.model_copy(update={"invocation_id": uuid4()}),
        )
    assert error.value.unknown
    before = len([row for row in h.adapter.calls if row[0] == "create"])
    with pytest.raises(ToolExecutionError) as retry:
        call(
            h,
            "tasks_publish",
            body,
            context=h.context.model_copy(update={"invocation_id": uuid4()}),
        )
    assert retry.value.unknown
    assert len([row for row in h.adapter.calls if row[0] == "create"]) == before
    assert h.bridge.operations(h.actor, h.project_id)[0].state in {"succeeded", "unknown"}
    assert h.bridge.get(h.actor, h.project_id).pending_operation_ids


def test_status_and_progress_grants_are_independent_and_runtime_derived(tools):
    h = tools
    assert call(h, "status_sync", {"expected_version": h.bound.version})["requires_setup"]
    h.bridge.bind(
        h.actor,
        h.project_id,
        BindProjectBoard(
            expected_version=h.bound.version,
            binding=h.binding.model_copy(update={"sync_status": True}),
        ),
    )
    add(h)
    publish(h, ["research"])
    runtime(h, "research", status="done", progress=100, result="Verified report", run_id=uuid4())
    h.work.request_cycle(h.actor, h.project_id, "Continue work", "runtime-sync-cycle")
    h.adapter.calls.clear()
    status = call(
        h,
        "status_sync",
        {"expected_version": h.bridge.get(h.actor, h.project_id).version, "todo_id": "research"},
    )
    assert status["changed"]
    assert [row[0] for row in h.adapter.calls].count("status") == 1
    assert not any(row[0] == "comment" for row in h.adapter.calls)
    progress = call(
        h,
        "progress_sync",
        {
            "expected_version": h.bridge.get(h.actor, h.project_id).version,
            "todo_id": "research",
        },
    )
    assert progress["changed"]
    assert [row[0] for row in h.adapter.calls].count("status") == 1
    comments = [row for row in h.adapter.calls if row[0] == "comment"]
    assert len(comments) == 1 and "Verified report" in comments[0][2]
    assert (
        call(
            h,
            "progress_sync",
            {
                "expected_version": h.bridge.get(h.actor, h.project_id).version,
            },
        )["changed"]
        is False
    )


def test_cancellation_after_remote_read_prevents_status_write(tools, monkeypatch):
    h = tools
    h.bridge.bind(
        h.actor,
        h.project_id,
        BindProjectBoard(
            expected_version=h.bound.version,
            binding=h.binding.model_copy(update={"sync_status": True}),
        ),
    )
    add(h)
    publish(h, ["research"])
    runtime(h, "research", status="done", progress=100)
    original = h.adapter.get_task

    def cancel_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        h.runs.cancel(h.actor, h.run.id)
        return result

    monkeypatch.setattr(h.adapter, "get_task", cancel_after_read)
    h.adapter.calls.clear()
    with pytest.raises(AuthorizationError, match="no longer active"):
        call(h, "status_sync", {"expected_version": h.bridge.get(h.actor, h.project_id).version})
    assert not any(row[0] == "status" for row in h.adapter.calls)
    assert any(
        row.kind == "status" and row.state == "failed"
        for row in h.bridge.operations(h.actor, h.project_id)
    )


def test_contract_and_network_flags_cannot_be_downgraded(tools):
    h = tools
    definition = h.definitions["clickup.tasks_publish"]
    for changes in (
        {"side_effect": False},
        {"required_scopes": frozenset()},
        {"action_policy": "read"},
        {"settings": {"network": False}},
    ):
        modified = definition.model_copy(update=changes)
        with pytest.raises(ToolCatalogError):
            h.handler(modified, {}, h.context)
        assert integration_status(modified, h.actor, {})[0] == "unconfigured"
        assert uses_network(modified)
    with pytest.raises(AuthorizationError):
        call(h, "tasks_publish", context=h.context.model_copy(update={"authorized_action": "read"}))


def test_real_dispatcher_executes_only_granted_clickup_tools_with_fake_provider(tools, monkeypatch):
    h = tools
    add(h)
    stages = 0

    def respond(request):
        nonlocal stages
        stages += 1
        if stages == 1:
            return json.dumps({"type": "tool", "tool_id": "clickup.project_read", "arguments": {}})
        if stages == 2:
            assert '"list_id":"456"' in request.prompt
            return json.dumps(
                {
                    "type": "tool",
                    "tool_id": "clickup.tasks_publish",
                    "arguments": {
                        "todo_ids": ["research"],
                        "expected_version": h.bridge.get(h.actor, h.project_id).version,
                    },
                }
            )
        assert '"status":"succeeded"' in request.prompt
        return json.dumps(
            {"type": "final", "output": "Published the project task; receipt verified."}
        )

    model = ControlledModel(respond)
    monkeypatch.setattr(
        "simon.services.agent_dispatcher.ModelEndpointClient", lambda *_, **__: model
    )
    plan = h.platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id="studio",
            project_id=h.project_id,
            idempotency_key="clickup-granted-plan",
            tasks=(
                AgentTaskSpec(
                    id="publish",
                    agent_id="writer",
                    objective="Publish research",
                    tool_ids=("clickup.project_read", "clickup.tasks_publish"),
                ),
            ),
        ),
    )
    assert plan.state == "planned", plan
    queued = h.runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="clickup-granted-run"))
    dispatcher = AgentDispatcher(
        h.runs,
        transport_factory=project_board_transport_factory(
            lambda *_: {},
            h.bridge,
            h.runs,
        ),
    )
    finished = dispatcher.execute(queued.id)
    assert finished.status == "succeeded", finished
    assert finished.tasks[0].tool_calls == 2
    assert len([row for row in h.adapter.calls if row[0] == "create"]) == 1
    assert h.bridge.get(h.actor, h.project_id).mappings[0].todo_id == "research"


def test_account_read_skills_work_without_project_list_binding(tools):
    h = tools
    h.adapter.task("account_task", description="Full account task")
    original_get = h.bridge.get
    h.bridge.get = lambda actor, project: original_get(actor, project).model_copy(
        update={"binding": None}
    )
    connections = call(h, "connections_list")
    connection = connections["connections"][0]["id"]
    boards = call(h, "boards_list", {"connection_id": connection})
    assert boards["boards"][0]["id"] == "456"
    listed = call(h, "account_tasks_list", {"connection_id": connection, "list_id": "456"})
    assert listed["tasks"][0]["id"] == "account_task"
    read = call(
        h,
        "account_task_read",
        {"connection_id": connection, "list_id": "456", "task_id": "account_task"},
    )
    assert read["description"] == "Full account task"
    assert read["untrusted_source"]
    assert call(h, "tasks_list")["status"] == "unavailable"
