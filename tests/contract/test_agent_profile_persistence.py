"""Durable custom-agent revisions and visibility between independent server instances."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_profiles import CreateAgentProfile, UpdateAgentProfile
from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import InvalidTransitionError
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService


@pytest.fixture
def custom_agents(store, tmp_path):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    manifest = PlatformManifest(
        agents=(
            AgentProfile(id="analyst", name="Analysis", instructions="Analyze supplied facts."),
        ),
        teams=(TeamTemplate(id="analysis", name="Analysis", agent_ids=("analyst",)),),
        models=(
            ModelEndpoint(
                id="local",
                provider="openai_compatible",
                local=True,
                model="test-local",
                tier="economy",
                base_url="http://localhost:11434/v1",
            ),
        ),
    )

    def instance():
        # PostgreSQL stores have independent transaction context and fresh connections.
        from simon.adapters.postgres import PostgresStore

        other_store = (
            PostgresStore(store._database_url) if isinstance(store, PostgresStore) else store
        )
        return AgentPlatformService(other_store, manifest, state_dir=tmp_path, environ={})

    api, worker = instance(), instance()
    skills = api.agent_profiles.skills(actor)
    assert skills
    return actor, api, worker, skills[0]["id"]


def test_saved_agent_is_visible_without_restarting_existing_worker(custom_agents):
    actor, api, worker, skill = custom_agents
    assert worker.agent_profiles.list(actor) == ()
    record = api.agent_profiles.create(
        actor,
        CreateAgentProfile(
            name="Brand strategist",
            description="Develop and maintain the brand launch plan.",
            skill_ids=(skill,),
            idempotency_key="brand-strategist-creation",
        ),
    )
    assert worker.agent_profiles.get(actor, record.id) == record
    plan = api.plan(
        actor,
        PlanTeamRequest(
            team_id="my-custom-agents",
            idempotency_key="brand-strategist-plan",
            tasks=(
                AgentTaskSpec(
                    id="strategy",
                    agent_id=record.id,
                    objective="Write a launch strategy for the supplied brief",
                ),
            ),
        ),
    )
    assert plan.state == "planned"
    runs = AgentRunService(worker, enabled=True, actor_resolver=lambda *_: actor)
    run = runs.start(actor, plan.id, StartAgentRun(idempotency_key="brand-strategist-run"))
    assert runs.live_actor(runs.job(run.id)).actor_id == actor.actor_id
    assert worker.plan_profiles(actor, plan)[record.id].description == record.description

    api.agent_profiles.update(
        actor,
        record.id,
        UpdateAgentProfile(
            name="Brand director",
            description="Own the brand strategy and document decisions.",
            skill_ids=(skill,),
            expected_version=record.version,
            idempotency_key="brand-strategist-edit",
        ),
    )
    assert worker.agent_profiles.get(actor, record.id).name == "Brand director"
    with pytest.raises(InvalidTransitionError, match=r"profile changed|unavailable"):
        runs.live_actor(runs.job(run.id))
    # Profile edits cannot erase historical plans or reports.
    assert worker.get(actor, plan.id) == plan


def test_concurrent_profile_edits_have_one_winner(custom_agents):
    actor, api, worker, skill = custom_agents
    record = api.agent_profiles.create(
        actor,
        CreateAgentProfile(
            name="Researcher",
            description="Research supplied materials.",
            skill_ids=(skill,),
            idempotency_key="concurrent-profile-create",
        ),
    )

    def edit(index):
        platform = api if index == 0 else worker
        try:
            return platform.agent_profiles.update(
                actor,
                record.id,
                UpdateAgentProfile(
                    name=f"Editor {index}",
                    description="Research and document conclusions.",
                    skill_ids=(skill,),
                    expected_version=record.version,
                    idempotency_key=f"concurrent-profile-edit-{index}",
                ),
            ).name
        except InvalidTransitionError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, (0, 1)))
    assert results.count("conflict") == 1
    assert api.agent_profiles.get(actor, record.id).name in results
