import os
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticError

from simon.adapters.browser_tools import browser_tool_definitions
from simon.adapters.memory import InMemoryStore
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
    WorkContext,
)
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from simon.domain.execution import EnvironmentDefinition
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel, JobStatus
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService, load_manifest
from simon.services.audit import AuditService
from simon.services.jobs import JobService


@pytest.fixture
def actor():
    return ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )


@pytest.fixture
def manifest():
    return PlatformManifest(
        max_parallel=4,
        agents=(
            AgentProfile(
                id="researcher",
                instructions="Research with evidence.",
                environment_ids=("sandbox",),
            ),
            AgentProfile(id="reviewer", instructions="Review the result independently."),
        ),
        teams=(
            TeamTemplate(
                id="research",
                name="Research team",
                agent_ids=("researcher", "reviewer"),
                max_parallel=3,
            ),
        ),
        models=(
            ModelEndpoint(
                id="local-small",
                provider="openai_compatible",
                model="test-model",
                base_url="http://127.0.0.1:11434/v1",
                local=True,
                tier="economy",
            ),
        ),
        environments=(
            EnvironmentDefinition(
                id="sandbox",
                kind="docker",
                enabled=True,
                container_image="python:3.11-slim",
                max_concurrency=1,
            ),
        ),
    )


def request(**updates):
    values = {
        "team_id": "research",
        "idempotency_key": "research-plan-001",
        "tasks": (
            AgentTaskSpec(id="a", agent_id="researcher", objective="Research option A"),
            AgentTaskSpec(id="b", agent_id="researcher", objective="Research option B"),
            AgentTaskSpec(
                id="review",
                agent_id="reviewer",
                objective="Compare the evidence",
                depends_on=("a", "b"),
            ),
        ),
    }
    values.update(updates)
    return PlanTeamRequest(**values)


@pytest.mark.parametrize("selected", [(), ("test.read",)])
def test_explicit_tools_without_environment_requirements_skip_optional_container(
    actor,
    manifest,
    tmp_path,
    selected,
):
    tool = ToolDefinition(
        id="test.read",
        description="Read application state",
        transport="http",
        configured=True,
        endpoint="https://example.test/read",
    )
    manifest = manifest.model_copy(
        update={
            "tools": (tool,),
            "agents": (
                manifest.agents[0].model_copy(update={"tool_ids": (tool.id,)}),
                manifest.agents[1],
            ),
            "environments": (manifest.environments[0].model_copy(update={"enabled": False}),),
            "models": (
                manifest.models[0].model_copy(
                    update={"capabilities": frozenset({"text", "tools"})},
                ),
            ),
        }
    )
    service = AgentPlatformService(InMemoryStore(), manifest, state_dir=tmp_path, environ={})
    plan = service.plan(
        actor,
        request(
            tasks=(
                AgentTaskSpec(
                    id="research",
                    agent_id="researcher",
                    objective="Read the requested context",
                    tool_ids=selected,
                ),
            )
        ),
    )
    assert plan.state == "planned", plan.tasks[0].blocked_reasons
    assert plan.tasks[0].environment is None
    explicit = service.plan(
        actor,
        request(
            idempotency_key="explicit-disabled-environment",
            tasks=(
                AgentTaskSpec(
                    id="research",
                    agent_id="researcher",
                    objective="Use chosen workspace",
                    tool_ids=selected,
                    environment_id="sandbox",
                ),
            ),
        ),
    )
    assert explicit.state == "blocked"
    assert "No permitted execution environment" in explicit.tasks[0].blocked_reasons[0]


def service(tmp_path, manifest, store=None):
    return AgentPlatformService(
        store or InMemoryStore(),
        manifest,
        state_dir=tmp_path / "state",
        environ={},
    )


@pytest.mark.parametrize(
    "tools,grants,explicit,web_enabled,expected_environment,expected_state",
    [
        (
            ("browser.read",),
            ("browser-offline", "browser-web"),
            None,
            True,
            "browser-web",
            "planned",
        ),
        (
            ("browser.screenshot",),
            ("browser-offline", "browser-web"),
            None,
            True,
            "browser-web",
            "planned",
        ),
        (
            ("browser.render_html",),
            ("browser-web", "browser-offline"),
            None,
            True,
            "browser-offline",
            "planned",
        ),
        (("browser.read",), ("browser-offline",), None, True, None, "blocked"),
        (("browser.read",), ("browser-offline", "browser-web"), None, False, None, "blocked"),
        (
            ("browser.read",),
            ("browser-offline", "browser-web"),
            "browser-offline",
            True,
            "browser-offline",
            "blocked",
        ),
        (
            ("browser.read", "browser.render_html"),
            ("browser-offline", "browser-web"),
            None,
            True,
            None,
            "blocked",
        ),
    ],
)
def test_browser_environment_selection_matches_network_without_expanding_grants(
    actor,
    manifest,
    tmp_path,
    tools,
    grants,
    explicit,
    web_enabled,
    expected_environment,
    expected_state,
):
    definitions = tuple(
        tool.model_copy(update={"settings": {"allowed_origins": ["https://example.com"]}})
        for tool in browser_tool_definitions(enabled=True)
    )
    configured = manifest.model_copy(
        update={
            "tools": definitions,
            "agents": (
                manifest.agents[0].model_copy(
                    update={
                        "tool_ids": tuple(tool.id for tool in definitions),
                        "tool_scopes": actor.scopes,
                        "max_action": "write",
                        "environment_ids": grants,
                    }
                ),
                manifest.agents[1],
            ),
            "environments": tuple(
                EnvironmentDefinition(
                    id=identifier,
                    kind="docker",
                    enabled=web_enabled if network == "bridge" else True,
                    container_image="simon-browser:local",
                    capabilities=frozenset({"browser", "python"}),
                    network=network,
                )
                for identifier, network in (("browser-offline", "none"), ("browser-web", "bridge"))
            ),
            "models": (
                manifest.models[0].model_copy(
                    update={"capabilities": frozenset({"text", "tools"})}
                ),
            ),
        }
    )
    platform = AgentPlatformService(
        InMemoryStore(),
        configured,
        state_dir=tmp_path / "state",
        environ={},
        available_transports=("browser",),
    )
    plan = platform.plan(
        actor,
        request(
            tasks=(
                AgentTaskSpec(
                    id="sources",
                    agent_id="researcher",
                    objective="Read the assigned source page",
                    tool_ids=tools,
                    environment_id=explicit,
                ),
            )
        ),
    )
    selected = plan.tasks[0]
    assert plan.state == expected_state, selected.blocked_reasons
    assert (
        selected.environment.environment_id if selected.environment else None
    ) == expected_environment
    if selected.environment:
        assert selected.environment.environment_id in grants
    assert platform.manifest == configured
    assert not (tmp_path / "state").exists()  # Planning never allocates a browser.


def test_plan_persists_without_allocating_or_starting_execution(tmp_path, manifest, actor):
    platform = service(tmp_path, manifest)
    plan = platform.plan(actor, request())
    assert plan.state == "planned" and not plan.execution_started
    assert plan.waves == (("a",), ("b",), ("review",))  # Container capacity is one.
    assert plan.tasks[0].model.endpoint_id == "local-small"
    assert plan.tasks[0].environment.workspace_path != plan.tasks[1].environment.workspace_path
    assert not (tmp_path / "state").exists()
    saved = platform.store.get_job(plan.id)
    assert saved.result is None  # PostgreSQL create_job persists input, not result.
    assert saved.status == JobStatus.WAITING
    restored = service(tmp_path, manifest, platform.store).get(actor, plan.id)
    assert restored == plan
    assert platform.plan(actor, request()) == plan


def test_idempotency_includes_actor_and_rejects_changed_request(tmp_path, manifest, actor):
    platform = service(tmp_path, manifest)
    first = platform.plan(actor, request(idempotency_key="x" * 180))
    with pytest.raises(IdempotencyConflictError):
        platform.plan(actor, request(idempotency_key="x" * 180, max_parallel=1))
    other = actor.model_copy(update={"actor_id": uuid4()})
    second = platform.plan(other, request(idempotency_key="x" * 180))
    assert first.id != second.id
    assert platform.list(other) == (second,)
    with pytest.raises(NotFoundError):
        platform.get(other, first.id)
    with pytest.raises(NotFoundError):
        platform.get(actor.model_copy(update={"workspace_id": uuid4()}), first.id)


@pytest.mark.parametrize(
    "tasks",
    [
        [{"id": "a", "agent_id": "researcher", "objective": "A", "depends_on": ["missing"]}],
        [{"id": "a", "agent_id": "researcher", "objective": "A", "depends_on": ["a"]}],
        [
            {"id": "a", "agent_id": "researcher", "objective": "A", "depends_on": ["b"]},
            {"id": "b", "agent_id": "researcher", "objective": "B", "depends_on": ["a"]},
        ],
        [
            {"id": "a", "agent_id": "researcher", "objective": "A"},
            {"id": "a", "agent_id": "researcher", "objective": "B"},
        ],
    ],
)
def test_invalid_dependency_graphs_fail_before_planning(tasks):
    with pytest.raises(PydanticError):
        request(tasks=tasks)


def test_important_task_blocks_without_frontier_and_dependent_does_not_run(
    tmp_path, manifest, actor
):
    platform = service(tmp_path, manifest)
    plan = platform.plan(
        actor,
        request(
            tasks=(
                AgentTaskSpec(
                    id="a", agent_id="reviewer", objective="Critical analysis", importance=5
                ),
                AgentTaskSpec(
                    id="b", agent_id="reviewer", objective="Use analysis", depends_on=("a",)
                ),
            )
        ),
    )
    assert plan.state == "blocked" and plan.waves == ()
    assert "frontier" in plan.tasks[0].blocked_reasons[0]


def test_task_cannot_widen_agent_grants(tmp_path, manifest, actor):
    platform = service(tmp_path, manifest)
    for changes in ({"tool_ids": ("anything",)}, {"environment_id": "ungranted"}):
        with pytest.raises(AuthorizationError):
            platform.plan(
                actor,
                request(
                    tasks=(AgentTaskSpec(id="a", agent_id="reviewer", objective="Work", **changes),)
                ),
            )


def test_local_policy_cannot_be_overridden_by_task(tmp_path, manifest, actor):
    data = manifest.model_dump(mode="json")
    data["agents"][1]["privacy"] = "local_only"
    platform = service(tmp_path, PlatformManifest.model_validate(data))
    with pytest.raises(AuthorizationError):
        platform.plan(
            actor,
            request(
                tasks=(
                    AgentTaskSpec(
                        id="a", agent_id="reviewer", objective="Work", privacy="allow_cloud"
                    ),
                )
            ),
        )


def test_scope_authorization_and_context_revocation(tmp_path, manifest, actor):
    context = WorkContext(
        id="brand",
        kind="company",
        name="Brand",
        workspace_id=actor.workspace_id,
        actor_ids=frozenset({actor.actor_id}),
    )
    configured = manifest.model_copy(update={"contexts": (context,)})
    platform = service(tmp_path, configured)
    plan = platform.plan(actor, request(context_id="brand"))
    with pytest.raises(NotFoundError):
        platform.plan(actor.model_copy(update={"actor_id": uuid4()}), request(context_id="brand"))
    with pytest.raises(NotFoundError):
        service(tmp_path, manifest, platform.store).get(actor, plan.id)
    with pytest.raises(AuthorizationError):
        platform.catalog(actor.model_copy(update={"scopes": frozenset()}))


def test_missing_tool_credentials_are_a_planning_blocker(tmp_path, manifest, actor):
    data = manifest.model_dump(mode="json")
    data["tools"] = [
        ToolDefinition(
            id="web.read",
            description="Read sources",
            transport="http",
            configured=True,
            endpoint="https://example.com/read",
            credential_env="PRIVATE_TOOL_KEY",
        ).model_dump(mode="json")
    ]
    data["agents"][1]["tool_ids"] = ["web.read"]
    data["models"][0]["capabilities"] = ["text", "tools"]
    platform = service(tmp_path, PlatformManifest.model_validate(data))
    plan = platform.plan(
        actor,
        request(tasks=(AgentTaskSpec(id="a", agent_id="reviewer", objective="Read sources"),)),
    )
    assert plan.state == "blocked"
    assert "missing its configured credential" in str(plan.tasks[0].blocked_reasons)


def test_raw_jobs_cannot_forge_or_expose_platform_plans(tmp_path, manifest, actor):
    platform = service(tmp_path, manifest)
    jobs = JobService(platform.store, AuditService(platform.store))
    with pytest.raises(ValidationError):
        jobs.submit(actor, kind="platform.plan", input={}, idempotency_key="forged-plan")
    plan = platform.plan(actor, request())
    with pytest.raises(NotFoundError):
        jobs.get(actor, plan.id)


def test_manifest_load_is_safe_and_references_are_validated(tmp_path):
    assert load_manifest(None) == PlatformManifest()
    path = tmp_path / "broken.json"
    path.write_text('{"secret": "do-not-echo-this"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid or unavailable") as error:
        load_manifest(path)
    assert "do-not-echo" not in str(error.value)
    with pytest.raises(PydanticError):
        PlatformManifest(agents=(AgentProfile(id="a", instructions="Work", tool_ids=("bad",)),))


def test_concurrency_override_is_bounded_and_parallel_work_is_grouped(tmp_path, manifest, actor):
    platform = service(tmp_path, manifest)
    plan = platform.plan(
        actor,
        request(
            max_parallel=128,
            tasks=tuple(
                AgentTaskSpec(id=f"t{index}", agent_id="reviewer", objective="Independent work")
                for index in range(5)
            ),
        ),
    )
    assert plan.max_parallel == 3
    assert plan.waves == (("t0", "t1", "t2"), ("t3", "t4"))


def test_request_fingerprint_survives_process_hash_seed_changes():
    code = """
from simon.domain.agent_platform import AgentTaskSpec
from simon.services.agent_platform import stable_configuration
from simon.services.canonical import digest
task = AgentTaskSpec(id='a', agent_id='worker', objective='Work',
                    model_capabilities=frozenset({'text', 'tools', 'vision'}))
print(digest(stable_configuration(task.model_dump(mode='python'))))
"""
    results = [
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            timeout=20,
        ).stdout
        for seed in (1, 2)
    ]
    assert results[0] == results[1]


def test_task_final_output_schema_is_optional_and_captures_valid_inline_schema():
    base = {"id": "plan", "agent_id": "reviewer", "objective": "Prepare a plan"}
    assert AgentTaskSpec(**base).final_output_schema is None
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["plan", "waiting", "complete"]}},
        "required": ["status"],
        "additionalProperties": False,
    }
    task = AgentTaskSpec(**base, final_output_schema=schema)
    assert task.final_output_schema == schema
    restored = AgentTaskSpec.model_validate_json(task.model_dump_json())
    assert restored.final_output_schema == schema
    schema["required"].clear()
    assert task.final_output_schema["required"] == ["status"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"type": "object", "required": "status"},
        {"type": "object", "description": "x" * 64000},
        {"type": "object", "properties": {"status": {"$ref": "https://example.com/schema"}}},
        {"type": "object", "properties": {"status": {"$dynamicRef": "file:///schema.json"}}},
        {"type": "object", "properties": {"status": {"$ref": "relative-schema.json#value"}}},
        {"type": "object", "default": float("nan")},
        {"type": "object", "$schema": "https://example.com/custom-dialect"},
        {"type": "object", "$schema": []},
        {"type": "object", "$schema": {}},
    ],
    ids=[
        "non-object",
        "invalid-schema",
        "oversized",
        "remote-ref",
        "dynamic-file-ref",
        "relative-ref",
        "nonfinite",
        "unknown-dialect",
        "list-dialect",
        "object-dialect",
    ],
)
def test_task_final_output_schema_rejects_invalid_unbounded_or_external_data(schema):
    with pytest.raises(PydanticError):
        AgentTaskSpec(
            id="plan", agent_id="reviewer", objective="Prepare a plan", final_output_schema=schema
        )


def test_task_final_output_schema_rejects_cyclic_python_input():
    schema = {"type": "object"}
    schema["properties"] = schema
    with pytest.raises(PydanticError, match="valid, finite JSON data"):
        AgentTaskSpec(
            id="plan", agent_id="reviewer", objective="Prepare a plan", final_output_schema=schema
        )


@pytest.mark.parametrize("reference", ["$ref", "$dynamicRef"])
def test_task_final_output_schema_rejects_local_refs_before_controller_embedding(reference):
    schema = {
        "type": "object",
        "properties": {"status": {reference: "#/$defs/status"}},
        "$defs": {"status": {"type": "string"}},
    }
    with pytest.raises(PydanticError, match="must inline all schema references"):
        AgentTaskSpec(
            id="plan", agent_id="reviewer", objective="Prepare a plan", final_output_schema=schema
        )
