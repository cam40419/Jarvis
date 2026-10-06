"""Roles remain reusable while live workspace connections change."""

from uuid import uuid4

import httpx
import pytest

from simon.adapters.github_tools import github_tool_definitions
from simon.adapters.memory import InMemoryStore
from simon.adapters.optional_http import BoundedHTTP
from simon.config import Settings
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_profiles import CreateAgentProfile
from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import InvalidTransitionError
from simon.domain.integrations import ConnectIntegration
from simon.domain.model_routing import ModelEndpoint
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.integrations import IntegrationService
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task
from tests.unit.test_external_actions import actor


def test_role_only_agent_and_project_member_share_tools_without_optional_setup(tmp_path):
    h = make_harness(tmp_path, tools=True)
    h.platform.shared_tools = h.platform.agent_profiles.shared_tools = True
    missing = ToolDefinition(
        id="optional", description="Optional account", transport="test", enabled=False
    )
    h.platform.manifest = h.platform.manifest.model_copy(
        update={"tools": (*h.platform.manifest.tools, missing)}
    )
    h.platform.agent_profiles.manifest = h.platform.manifest
    created = h.platform.agent_profiles.create(
        h.actor,
        CreateAgentProfile(
            name="Research lead",
            description="Research, save the report and verify it.",
            idempotency_key="role-only-create",
        ),
    )
    assert created.skill_ids == ()
    assert created.profile.tool_access == "shared"
    assert set(created.profile.tool_ids) == {"lookup", "optional"}
    snapshot = h.platform.agent_profiles.capture_role(
        h.actor, "member", created.name, created.description, (), 1
    )
    member = h.platform.agent_profiles.resolve_role(h.actor, snapshot)
    assert member.instructions.count(created.description) == 1
    assert set(member.tool_ids) == {"lookup", "optional"}
    planned = h.plan((task("research"),))
    assert planned.tasks[0].tool_ids == ("lookup",)
    assert not planned.tasks[0].blocked_reasons


def test_unrelated_configuration_changes_do_not_invalidate_a_saved_assignment(tmp_path):
    h = make_harness(tmp_path, tools=True)
    queued = h.queue((task("research").model_copy(update={"tool_ids": ()}),))
    unrelated = ToolDefinition(
        id="unused", description="New integration", transport="test", enabled=False
    )
    h.platform.manifest = h.platform.manifest.model_copy(
        update={
            "max_parallel": 4,
            "tools": (*h.platform.manifest.tools, unrelated),
            "agents": (
                *h.platform.manifest.agents,
                AgentProfile(id="other", instructions="Another role."),
            ),
        }
    )
    finished = h.dispatcher(ControlledModel()).execute(queued.id)
    assert finished.status == "succeeded"


def test_optional_tool_disabled_after_queueing_is_omitted_from_worker_input(tmp_path):
    h = make_harness(tmp_path, tools=True)
    h.platform.shared_tools = h.platform.agent_profiles.shared_tools = True
    queued = h.queue((task("research"),))
    definitions = tuple(
        tool.model_copy(update={"enabled": False}) for tool in h.platform.manifest.tools
    )
    h.platform.manifest = h.platform.manifest.model_copy(update={"tools": definitions})
    model = ControlledModel()
    finished = h.dispatcher(model).execute(queued.id)
    assert finished.status == "succeeded"
    assert not model.calls[0].controller_mode
    assert "Granted tools:" not in model.calls[0].system


def test_used_tool_contract_changes_still_fence_execution(tmp_path):
    h = make_harness(tmp_path, tools=True)
    plan = h.plan((task("research"),))
    changed = h.platform.manifest.tools[0].model_copy(update={"action_policy": "write"})
    h.platform.manifest = h.platform.manifest.model_copy(update={"tools": (changed,)})
    with pytest.raises(InvalidTransitionError, match="execution contract"):
        h.runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="changed-tool-run"))


def test_role_and_team_edits_apply_to_future_work_without_interrupting_saved_work(tmp_path):
    h = make_harness(tmp_path)
    h.platform.shared_tools = True
    plan = h.plan((task("research"),))
    saved_instructions = h.platform.plan_profiles(h.actor, plan)["worker"].instructions
    edited = h.platform.manifest.agents[0].model_copy(
        update={"instructions": "New role instructions."}
    )
    team = h.platform.manifest.teams[0].model_copy(update={"version": 2})
    h.platform.manifest = h.platform.manifest.model_copy(
        update={"agents": (edited,), "teams": (team,)}
    )
    h.runs.assert_team(h.actor, plan)
    assert h.platform.plan_profiles(h.actor, plan)["worker"].instructions == saved_instructions
    queued = h.runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="finish-saved-role"))
    assert h.dispatcher(ControlledModel()).execute(queued.id).status == "succeeded"


def test_new_model_does_not_replace_a_model_in_an_existing_assignment(tmp_path):
    h = make_harness(tmp_path)
    queued = h.queue((task("research"),))
    new = h.platform.manifest.models[0].model_copy(update={"id": "new-model", "priority": 0})
    h.platform.manifest = h.platform.manifest.model_copy(
        update={"models": (*h.platform.manifest.models, new)}
    )
    h.platform.models.endpoints = h.platform.manifest.models
    assert h.dispatcher(ControlledModel()).execute(queued.id).status == "succeeded"


def test_unconfigured_tools_are_omitted_and_future_plans_can_use_new_connections(tmp_path):
    current = actor()
    settings = Settings(_env_file=None, integration_key_file=tmp_path / "key")
    integrations = IntegrationService(
        InMemoryStore(),
        settings,
        http=BoundedHTTP(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"login": "owner"})
            )
        ),
    )
    manifest = PlatformManifest(
        agents=(AgentProfile(id="worker", instructions="Research repositories."),),
        teams=(TeamTemplate(id="team", name="Team", agent_ids=("worker",)),),
        tools=github_tool_definitions(),
        models=(
            ModelEndpoint(
                id="local",
                model="fake",
                provider="openai_compatible",
                base_url="http://localhost:1234/v1",
                local=True,
                capabilities=frozenset({"text", "tools"}),
            ),
        ),
    )
    platform = AgentPlatformService(
        integrations.store,
        manifest,
        state_dir=tmp_path / "state",
        integrations=integrations,
        available_transports=("github",),
    )
    request = PlanTeamRequest(
        team_id="team",
        idempotency_key="missing-github-plan",
        tasks=(
            AgentTaskSpec(
                id="inspect",
                agent_id="worker",
                objective="Inspect the repository",
                tool_ids=("github.repository",),
            ),
        ),
    )
    original = platform.plan(current, request)
    assert original.state == "planned"
    assert original.tasks[0].tool_ids == ()
    assert original.tasks[0].blocked_reasons == ()
    integrations.connect(
        current,
        "github",
        ConnectIntegration(credential="test-token", repositories=["owner/repository"]),
    )
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: current)
    refreshed = platform.plan(
        current, request.model_copy(update={"idempotency_key": "connected-github-plan"})
    )
    queued = runs.start(current, refreshed.id, StartAgentRun(idempotency_key="start-after-connect"))
    refreshed = platform.get(current, queued.plan_id)
    assert refreshed.state == "planned"
    assert refreshed.id != original.id
    assert refreshed.tasks[0].objective == original.tasks[0].objective
    assert refreshed.tasks[0].tool_ids == ("github.repository",)
    assert platform.get(current, original.id).tasks[0].tool_ids == ()


@pytest.mark.parametrize(
    "provider,settings",
    [
        ("dropbox", {"root_path": "/Simon"}),
        ("box", {"root_folder_id": "0"}),
        (
            "onedrive",
            {
                "drive_id": "drive",
                "root_item_id": "folder",
                "download_hosts": ["tenant.sharepoint.com"],
            },
        ),
        ("webdav", {"endpoint": "https://storage.example.com/folder", "username": "user"}),
    ],
)
def test_storage_link_test_and_live_grant_are_private_and_owner_scoped(
    tmp_path, provider, settings
):
    current = actor()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            207 if request.method == "PROPFIND" else 200, json={"type": "folder", ".tag": "folder"}
        )

    service = IntegrationService(
        InMemoryStore(),
        Settings(_env_file=None, integration_key_file=tmp_path / "key"),
        http=BoundedHTTP(transport=httpx.MockTransport(handle)),
    )
    from simon.agent_setup import starter_manifest

    platform = AgentPlatformService(
        service.store,
        starter_manifest(settings=service.settings),
        state_dir=tmp_path / "state",
        integrations=service,
        available_transports=(provider,),
    )
    public = service.connect(
        current, provider, ConnectIntegration(credential="private-token", **settings)
    )
    assert "private-token" not in str(public)
    statuses = {row["id"]: row for row in platform.tool_statuses(current)}
    assert statuses[provider + ".list"]["state"] == "configured"
    assert not any(request.method in {"PUT", "PATCH", "DELETE"} for request in requests)
    test = service.test(current, public["id"])
    assert test["status"] == "passed"
    assert service.list(current)[0]["settings"]["connection_test"] == test
    linked = service.store.integration_connections(current.workspace_id, current.actor_id)[0]
    service.store.save_integration_connection(
        linked.model_copy(
            update={"settings": {**linked.settings, "connection_test": {"status": "failed"}}}
        )
    )
    failed = next(row for row in platform.tool_statuses(current) if row["id"] == provider + ".list")
    assert failed["state"] == "unconfigured"
    assert "reconnect and test" in failed["blocked_reasons"][0]
    assert service.test(current, public["id"])["status"] == "passed"
    foreign = current.model_copy(update={"actor_id": uuid4()})
    assert (
        next(row for row in platform.tool_statuses(foreign) if row["id"] == provider + ".list")[
            "state"
        ]
        == "unconfigured"
    )
    service.disconnect(current, public["id"])
    assert (
        next(row for row in platform.tool_statuses(current) if row["id"] == provider + ".list")[
            "state"
        ]
        == "unconfigured"
    )


def test_admin_model_key_is_private_and_resolved_live_by_other_processes(tmp_path):
    current = actor()
    settings = Settings(
        _env_file=None,
        integration_key_file=tmp_path / "key",
        account_admin_actor_id=current.actor_id,
        account_workspace_id=current.workspace_id,
    )
    store = InMemoryStore()
    service = IntegrationService(
        store,
        settings,
        http=BoundedHTTP(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
        ),
    )
    other_process = IntegrationService(store, settings)
    public = service.connect(current, "openai", ConnectIntegration(credential="private-model-key"))
    assert "private-model-key" not in str(public)
    assert other_process.credentials["SIMON_OPENAI_API_KEY"] == "private-model-key"
    assert service.test(current, public["id"])["status"] == "passed"
    service.connect(
        current, "openai", ConnectIntegration(credential="rotated-model-key"), public["id"]
    )
    assert other_process.credentials["SIMON_OPENAI_API_KEY"] == "rotated-model-key"


def test_runtime_check_does_not_allocate_resources_or_execute_project_commands(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    h = make_harness(tmp_path, environment=True)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", run)
    assert h.platform.test_environment(h.actor, "shared")["status"] == "passed"
    assert commands == [["docker", "image", "inspect", "unused-test-image"]]
    assert h.backend.created == []


def test_connection_test_cannot_restore_a_rotated_credential(tmp_path, monkeypatch):
    current = actor()
    service = IntegrationService(
        InMemoryStore(),
        Settings(_env_file=None, integration_key_file=tmp_path / "key"),
        http=BoundedHTTP(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )
    public = service.connect(
        current, "box", ConnectIntegration(credential="old-token", root_folder_id="0")
    )
    original = service._probe_storage

    def rotate(record, secret):
        monkeypatch.setattr(service, "_probe_storage", original)
        service.connect(
            current,
            "box",
            ConnectIntegration(credential="new-token", root_folder_id="0"),
            public["id"],
        )

    monkeypatch.setattr(service, "_probe_storage", rotate)
    assert service.test(current, public["id"])["status"] == "configuration_changed"
    latest = service.store.integration_connections(current.workspace_id, current.actor_id)[0]
    assert service.decrypt(latest.encrypted_secret) == "new-token"
    assert "connection_test" not in latest.settings
