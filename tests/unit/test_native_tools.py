import json
from uuid import uuid4

import pytest

from simon.adapters.google import DRIVE_READ_SCOPE, ConnectedError
from simon.adapters.memory import InMemoryStore
from simon.adapters.model_endpoints import ModelEndpointClient
from simon.adapters.native_tools import (
    NativeToolTransport,
    native_tool_definitions,
    native_tool_status,
    native_transport_factory,
    with_native_tools,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import StartAgentRun
from simon.domain.context import CreateMemory
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.identity import Membership
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.domain.models import JobStatus
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import WorkerCheckpointError
from tests.contract.test_google_read_permissions import grant
from tests.contract.test_local_files import local_setup
from tests.contract.test_project_files import setup_project


@pytest.fixture
def runtime(tmp_path):
    store = InMemoryStore()
    connected, actor, files, host = local_setup(store, tmp_path)
    run_id = uuid4()
    transport = NativeToolTransport(
        connected,
        actor=actor,
        run_id=run_id,
        revalidate=lambda: actor,
    )
    definitions = {item.id: item for item in native_tool_definitions(connected)}
    registry = TransportRegistry()
    registry.register("native", transport)

    def invoke(name, arguments=None, *, action="write", invocation=None, scopes=None):
        definition = definitions["native." + name]
        context = ToolExecutionContext(
            actor_id=actor.actor_id,
            household_id=actor.household_id,
            run_id=run_id,
            agent_id="worker",
            invocation_id=invocation or uuid4(),
            allowed_tool_ids=frozenset({definition.id}),
            scopes=actor.scopes if scopes is None else frozenset(scopes),
            authorized_action=action,
        )
        return registry.execute(definition, arguments or {}, context).output

    return connected, actor, files, host, transport, invoke


def test_local_write_read_revision_and_invocation_idempotency(runtime):
    _, _, files, _, transport, invoke = runtime
    path = {"root": "workspace", "path": "reports/notes.md"}
    arguments = {**path, "content": "Original"}
    invocation = uuid4()
    first = invoke("local_file_write", arguments, invocation=invocation)
    assert first == invoke("local_file_write", arguments, invocation=invocation)
    assert invoke("local_file_read", path)["text"] == "Original"
    saved = invoke(
        "local_file_edit",
        {
            **path,
            "revision": first["revision"],
            "old_text": "Original",
            "new_text": "Updated",
        },
    )
    assert saved["revision"] != first["revision"]
    with pytest.raises(ValidationError, match="changed"):
        invoke("local_file_write", {**path, "content": "Stale", "revision": first["revision"]})
    assert (files.workspace(transport.actor) / "reports/notes.md").read_text() == "Updated"


def test_action_and_profile_scopes_cannot_be_bypassed(runtime):
    _, actor, _, _, transport, invoke = runtime
    arguments = {"root": "workspace", "path": "notes.md", "content": "Private"}
    with pytest.raises(AuthorizationError, match="action policy"):
        invoke("local_file_write", arguments, action="read")
    with pytest.raises(AuthorizationError, match="permission"):
        invoke("local_file_write", arguments, scopes={"jobs:read"})
    # Even a caller that skips TransportRegistry cannot downgrade the operation contract.
    definition = next(t for t in native_tool_definitions() if t.id == "native.local_file_write")
    definition = definition.model_copy(
        update={
            "side_effect": False,
            "action_policy": "read",
            "required_scopes": frozenset(),
        }
    )
    context = ToolExecutionContext(
        actor_id=actor.actor_id,
        household_id=actor.household_id,
        run_id=transport.run_id,
        agent_id="worker",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes,
    )
    with pytest.raises(AuthorizationError, match="write-authorized"):
        transport(definition, arguments, context)


@pytest.mark.parametrize("path", ["../outside.txt", "C:/Windows/win.ini", ".env", ".git/config"])
def test_local_paths_cannot_escape_or_reveal_protected_files(runtime, path):
    *_, invoke = runtime
    with pytest.raises((AuthorizationError, ValidationError)):
        invoke("local_file_read", {"root": "workspace", "path": path})


def test_agent_never_exposes_host_roots_even_for_server_owner(runtime):
    _, actor, files, host, transport, invoke = runtime
    (host / "host-only.txt").write_text("secret")
    assert "downloads" in files.roots(actor)
    roots = invoke("local_files_roots")
    assert roots["roots"] == [{"root": "workspace", "name": "My workspace"}]
    definition = next(t for t in native_tool_definitions() if t.id == "native.local_file_read")
    context = ToolExecutionContext(
        actor_id=actor.actor_id,
        household_id=actor.household_id,
        run_id=transport.run_id,
        agent_id="worker",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes,
    )
    with pytest.raises(AuthorizationError, match="workspace and project"):
        transport(definition, {"root": "downloads", "path": "host-only.txt"}, context)


def test_project_roots_require_visible_project_and_retained_scopes(runtime):
    connected, actor, _, _, _, invoke = runtime
    project = connected.memories.create(
        actor,
        CreateMemory(
            subject="Project",
            content="Private work",
            scope="personal",
            category="project",
            idempotency_key="native-project",
        ),
    )
    path = {"root": f"project:{project.id}", "path": "notes.md", "content": "project"}
    invoke("local_file_write", path)
    with pytest.raises(AuthorizationError, match="Project access"):
        invoke("local_file_write", path, scopes={"jobs:read", "jobs:write"})
    other = actor.model_copy(update={"actor_id": uuid4()})
    connected.store.put_membership(
        Membership(
            actor_id=other.actor_id,
            household_id=other.household_id,
            role="owner",
            display_name="Other",
            household_name="Household",
        )
    )
    tool = NativeToolTransport(
        connected,
        actor=other,
        run_id=uuid4(),
        revalidate=lambda: other,
    )
    definition = next(t for t in native_tool_definitions() if t.id == "native.local_file_read")
    context = ToolExecutionContext(
        actor_id=other.actor_id,
        household_id=other.household_id,
        run_id=tool.run_id,
        agent_id="worker",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=other.scopes,
    )
    with pytest.raises(NotFoundError, match="Project not found"):
        tool(definition, {"root": path["root"], "path": "notes.md"}, context)


def test_revalidation_rejects_owner_changes_and_cancellation(runtime):
    _, actor, _, _, transport, invoke = runtime
    transport.revalidate = lambda: actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(AuthorizationError, match="access changed"):
        invoke("local_files_roots")

    def cancelled():
        raise WorkerCheckpointError("cancelled")

    transport.revalidate = cancelled
    with pytest.raises(WorkerCheckpointError):
        invoke("local_file_write", {"root": "workspace", "path": "notes", "content": "x"})


def test_actor_scope_revocation_before_read_and_after_cloud_result(runtime):
    connected, actor, _, _, transport, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))

    def read(token, request):
        transport.revalidate = lambda: actor.model_copy(update={"scopes": frozenset()})
        return {"text": "Must not be released"}

    connected.api.drive_file = read
    with pytest.raises(AuthorizationError, match="permissions changed"):
        invoke("drive_read_file", {"id": "file"})
    with pytest.raises(AuthorizationError, match="permissions changed"):
        invoke("local_files_roots")


def test_google_reads_are_scoped_to_connected_accounts(runtime):
    connected, _, _, _, _, invoke = runtime
    grant(connected, runtime[1], (DRIVE_READ_SCOPE,))
    calls = []
    connected.api.drive_search = lambda token, request: calls.append(request) or {"files": []}
    result = invoke("drive_search_files", {"query": "notes"})
    assert result["account_email"] == "owner@example.com" and len(calls) == 1
    with pytest.raises(ConnectedError, match="not connected"):
        invoke("drive_search_files", {"account": "another@example.com"})
    assert len(calls) == 1


def test_project_reads_do_not_create_cloud_folders_and_writes_keep_receipts():
    store = InMemoryStore()
    connected, actor, files, project_id = setup_project(store)
    run_id = uuid4()
    transport = NativeToolTransport(connected, actor=actor, run_id=run_id, revalidate=lambda: actor)
    definitions = {item.id: item for item in native_tool_definitions(connected)}

    def invoke(name, arguments, invocation=None, write=False):
        definition = definitions["native." + name]
        context = ToolExecutionContext(
            actor_id=actor.actor_id,
            household_id=actor.household_id,
            run_id=run_id,
            agent_id="worker",
            allowed_tool_ids=frozenset({definition.id}),
            scopes=actor.scopes,
            invocation_id=invocation or uuid4(),
            authorized_action="write" if write else "read",
        )
        return transport(definition, arguments, context)

    arguments = {"project_id": str(project_id), "name": "report.md", "content": "Report"}
    invocation = uuid4()
    created = invoke("project_file_create", arguments, invocation, write=True)
    assert created == invoke("project_file_create", arguments, invocation, write=True)
    assert files.api.creates == 2  # One pre-existing project folder and one deliverable.
    read = invoke(
        "project_file_read",
        {
            "project_id": str(project_id),
            "file_id": created["file_id"],
        },
    )
    assert read["text"] == "Report"
    binding = files.binding(actor, project_id)
    store.save_project_drive(binding.model_copy(update={"folder_id": None}))
    with pytest.raises(ValidationError, match="Link a project"):
        invoke("project_files_list", {"project_id": str(project_id)})
    assert files.api.creates == 2


def test_unknown_cloud_write_halts_worker_without_replaying():
    # Use the real project operation receipt after a simulated lost upload response.
    connected, actor, files, project_id = setup_project(InMemoryStore())
    files.api.timeout_after_create = True
    transport = NativeToolTransport(
        connected,
        actor=actor,
        run_id=uuid4(),
        revalidate=lambda: actor,
    )
    definition = next(t for t in native_tool_definitions() if t.id == "native.project_file_create")
    context = ToolExecutionContext(
        actor_id=actor.actor_id,
        household_id=actor.household_id,
        run_id=transport.run_id,
        agent_id="worker",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes,
        authorized_action="write",
    )
    arguments = {"project_id": str(project_id), "name": "notes.md", "content": "Notes"}
    with pytest.raises(ToolExecutionError) as raised:
        transport(definition, arguments, context)
    assert raised.value.unknown
    with pytest.raises(ToolExecutionError):
        transport(definition, arguments, context)
    assert files.api.creates == 2


def test_manifest_binding_installs_only_explicit_ids_and_cannot_weaken_contract(runtime):
    connected, actor, _, _, _, _ = runtime
    canonical = next(t for t in native_tool_definitions() if t.id == "native.local_file_write")
    weak = canonical.model_copy(
        update={
            "side_effect": False,
            "action_policy": "read",
            "required_scopes": frozenset(),
        }
    )
    manifest = with_native_tools(PlatformManifest(tools=(weak,)), connected)
    assert manifest.tools == (canonical,)
    assert not with_native_tools(PlatformManifest(), connected).tools
    assert native_tool_status(connected, actor, canonical.id)["available"]
    assert not native_tool_status(connected, actor, "native.drive_read_file")["available"]
    with pytest.raises(ToolCatalogError, match="Unknown native"):
        with_native_tools(
            PlatformManifest(
                tools=(
                    canonical.model_copy(
                        update={
                            "id": "native.execute_shell",
                        }
                    ),
                )
            ),
            connected,
        )
    connected.settings = connected.settings.model_copy(update={"local_files_enabled": False})
    bound = with_native_tools(PlatformManifest(tools=(canonical,)), connected)
    assert not bound.tools[0].configured
    assert "native" in native_transport_factory(connected)(actor, uuid4(), lambda: actor)


def test_dispatcher_executes_native_tools_with_injected_services(runtime, tmp_path, monkeypatch):
    connected, actor, _, _, _, _ = runtime
    tool = next(t for t in native_tool_definitions() if t.id == "native.local_files_roots")
    manifest = PlatformManifest(
        tools=(tool,),
        agents=(
            AgentProfile(
                id="worker",
                instructions="List the local workspace roots.",
                tool_ids=(tool.id,),
                tool_scopes=frozenset({"jobs:read"}),
            ),
        ),
        teams=(TeamTemplate(id="team", name="Native tools", agent_ids=("worker",)),),
        models=(
            ModelEndpoint(
                id="local",
                provider="openai_compatible",
                model="test",
                base_url="http://localhost:11434/v1",
                local=True,
                tier="economy",
                capabilities=frozenset({"text", "tools"}),
            ),
        ),
    )
    platform = AgentPlatformService(
        connected.store,
        manifest,
        state_dir=tmp_path / "agents",
        available_transports=("native",),
    )
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: actor)
    plan = platform.plan(
        actor,
        PlanTeamRequest(
            team_id="team",
            tasks=(
                AgentTaskSpec(
                    id="roots",
                    agent_id="worker",
                    objective="List local roots",
                ),
            ),
            idempotency_key="native-tool-plan",
        ),
    )
    run = runs.start(actor, plan.id, StartAgentRun(idempotency_key="native-tool-run"))
    replies = iter(
        [
            {"type": "tool", "tool_id": tool.id, "arguments": {}},
            {"type": "final", "output": "The workspace is available."},
        ]
    )

    def generate(self, decision, request):
        return TextGenerationResult(
            endpoint_id=decision.endpoint_id,
            model=decision.model,
            text=json.dumps(next(replies)),
            input_tokens=10,
            output_tokens=10,
        )

    monkeypatch.setattr(ModelEndpointClient, "generate", generate)
    completed = AgentDispatcher(
        runs,
        transport_factory=native_transport_factory(connected),
    ).execute(run.id)
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.tasks[0].tool_calls == 1
    assert completed.tasks[0].output == "The workspace is available."


def test_cached_templates_are_owned_copies_and_settings_are_rechecked(runtime):
    connected, actor, _, _, _, _ = runtime
    first = next(t for t in native_tool_definitions(connected) if t.id == "native.local_file_read")
    first.input_schema["properties"]["root"].clear()
    first.settings["network"] = True
    second = next(t for t in native_tool_definitions(connected) if t.id == first.id)
    assert "pattern" in second.input_schema["properties"]["root"]
    assert second.settings["network"] is False
    connected.settings = connected.settings.model_copy(update={"local_files_enabled": False})
    assert not native_tool_status(connected, actor, first.id)["available"]
    assert not next(t for t in native_tool_definitions(connected) if t.id == first.id).configured
