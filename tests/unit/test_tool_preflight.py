from uuid import uuid4

import pytest

from simon.adapters.browser_tools import browser_tool_definitions
from simon.adapters.cad_tools import cad_tool_definitions
from simon.adapters.memory import InMemoryStore
from simon.adapters.pcb_tools import pcb_tool_definitions
from simon.adapters.tool_preflight import INSTALLED_TRANSPORTS
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.execution import EnvironmentDefinition
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel
from simon.services.agent_platform import AgentPlatformService


@pytest.mark.parametrize(
    "origins,network,ready",
    [
        ([], "bridge", False),
        (["http://example.com"], "bridge", False),
        (["https://example.com"], "none", False),
        (["https://example.com"], "bridge", True),
    ],
)
def test_browser_plan_checks_origins_and_environment_before_execution(
    tmp_path,
    origins,
    network,
    ready,
):
    definition = next(
        tool for tool in browser_tool_definitions(enabled=True) if tool.id == "browser.read"
    ).model_copy(
        update={"settings": {"allowed_origins": origins}},
    )
    actor = ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    platform = AgentPlatformService(
        InMemoryStore(),
        PlatformManifest(
            tools=(definition,),
            agents=(
                AgentProfile(
                    id="reader",
                    instructions="Read the requested source.",
                    tool_ids=(definition.id,),
                    tool_scopes=actor.scopes,
                    environment_ids=("browser",),
                ),
            ),
            teams=(TeamTemplate(id="web", name="Web", agent_ids=("reader",)),),
            models=(
                ModelEndpoint(
                    id="local",
                    model="test",
                    provider="openai_compatible",
                    base_url="http://127.0.0.1:11434/v1",
                    local=True,
                    capabilities=frozenset({"text", "tools"}),
                ),
            ),
            environments=(
                EnvironmentDefinition(
                    id="browser",
                    kind="docker",
                    container_image="test-browser:local",
                    enabled=True,
                    network=network,
                    capabilities=frozenset({"python", "browser"}),
                ),
            ),
        ),
        state_dir=tmp_path,
        environ={},
        available_transports=INSTALLED_TRANSPORTS,
    )
    plan = platform.plan(
        actor,
        PlanTeamRequest(
            team_id="web",
            idempotency_key="browser-plan",
            tasks=(AgentTaskSpec(id="read", agent_id="reader", objective="Read an example page"),),
        ),
    )
    assert (plan.state == "planned") == ready
    assert not list(tmp_path.iterdir())  # Preflight allocates no browser and calls no provider.
    status = platform.tool_statuses(actor)[0]
    assert status["state"] == (
        "configured" if origins == ["https://example.com"] else "unconfigured"
    )


@pytest.mark.parametrize(
    "tool", [cad_tool_definitions(enabled=True)[0], pcb_tool_definitions(enabled=True)[0]]
)
@pytest.mark.parametrize("network,ready", [("none", True), ("bridge", False)])
def test_engineering_tools_require_offline_workers_before_execution(tmp_path, tool, network, ready):
    actor = ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    platform = AgentPlatformService(
        InMemoryStore(),
        PlatformManifest(
            tools=(tool,),
            agents=(
                AgentProfile(
                    id="engineer",
                    instructions="Create the requested model.",
                    tool_ids=(tool.id,),
                    tool_scopes=actor.scopes,
                    max_action="write",
                    environment_ids=("engineering",),
                ),
            ),
            teams=(TeamTemplate(id="engineering", name="Engineering", agent_ids=("engineer",)),),
            models=(
                ModelEndpoint(
                    id="local",
                    model="test",
                    provider="openai_compatible",
                    base_url="http://127.0.0.1:11434/v1",
                    local=True,
                    capabilities=frozenset({"text", "tools"}),
                ),
            ),
            environments=(
                EnvironmentDefinition(
                    id="engineering",
                    kind="docker",
                    container_image="engineering:test",
                    enabled=True,
                    network=network,
                    capabilities=tool.environment_capabilities,
                ),
            ),
        ),
        state_dir=tmp_path,
        environ={},
        available_transports=INSTALLED_TRANSPORTS,
    )
    plan = platform.plan(
        actor,
        PlanTeamRequest(
            team_id="engineering",
            idempotency_key="engineering-plan",
            tasks=(AgentTaskSpec(id="create", agent_id="engineer", objective="Create a model"),),
        ),
    )
    assert (plan.state == "planned") == ready
    assert not list(tmp_path.iterdir())
    if not ready:
        assert any("offline Linux Docker" in reason for reason in plan.tasks[0].blocked_reasons)
