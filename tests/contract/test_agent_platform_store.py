from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel
from simon.services.agent_platform import AgentPlatformService


def test_compiled_plan_round_trips_across_store_implementations(store, tmp_path):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    manifest = PlatformManifest(
        agents=(AgentProfile(id="writer", instructions="Create a draft."),),
        teams=(TeamTemplate(id="solo", name="Solo", agent_ids=("writer",)),),
        models=(ModelEndpoint(id="local", provider="openai_compatible", model="test",
                              base_url="http://localhost:11434/v1", local=True, tier="economy"),),
    )
    request = PlanTeamRequest(
        team_id="solo", idempotency_key="store-contract-plan",
        tasks=(AgentTaskSpec(id="draft", agent_id="writer", objective="Draft an outline"),),
    )
    original = AgentPlatformService(store, manifest, state_dir=tmp_path, environ={})
    plan = original.plan(actor, request)
    assert store.get_job(plan.id).result is None
    reconstructed = AgentPlatformService(store, manifest, state_dir=tmp_path, environ={})
    assert reconstructed.get(actor, plan.id) == plan
    assert reconstructed.plan(actor, request) == plan
