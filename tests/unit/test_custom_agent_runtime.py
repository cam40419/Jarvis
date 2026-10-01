from uuid import uuid4

import pytest

from simon.domain.agent_platform import AgentProfile, AgentTaskSpec, PlanTeamRequest, TeamTemplate
from simon.domain.agent_profiles import CreateAgentProfile
from simon.domain.errors import ValidationError
from simon.services.agent_platform import AgentPlatformService
from tests.unit.test_agent_dispatcher import make_harness


def test_custom_team_does_not_collide_with_operator_team(tmp_path):
    h = make_harness(tmp_path)
    stock = TeamTemplate(
        id="my-custom-agents",
        name="Operator's team",
        agent_ids=("worker",),
    )
    manifest = h.platform.manifest.model_copy(update={"teams": (stock,)})
    platform = AgentPlatformService(h.platform.store, manifest, state_dir=tmp_path, environ={})
    available = platform.agent_profiles.skills(h.actor)
    created = platform.agent_profiles.create(
        h.actor,
        CreateAgentProfile(
            name="Custom analyst",
            description="Analyze the requested material.",
            skill_ids=(available[0]["id"],),
            idempotency_key="custom-collision-test",
        ),
    )
    teams = platform.catalog(h.actor)["teams"]
    assert len({team["id"] for team in teams}) == len(teams) == 2
    personal = next(team for team in teams if created.profile.id in team["agent_ids"])
    assert personal["id"] != stock.id
    assert platform.resolve_team(h.actor, stock.id) == stock
    plan = platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id=personal["id"],
            idempotency_key="plan-custom-collision",
            tasks=(
                AgentTaskSpec(
                    id="analysis",
                    agent_id=created.profile.id,
                    objective="Analyze the supplied notes.",
                ),
            ),
        ),
    )
    assert plan.state == "planned" and plan.tasks[0].agent_id == created.profile.id


@pytest.mark.parametrize("hidden", [False, True])
def test_stock_profile_cannot_replace_saved_custom_id_in_existing_project(tmp_path, hidden):
    h = make_harness(tmp_path)
    created = h.platform.agent_profiles.create(
        h.actor,
        CreateAgentProfile(
            name="Project researcher",
            description="Research the project brief.",
            skill_ids=("analysis",),
            idempotency_key="saved-project-custom-agent",
        ),
    )
    original_plan = h.platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id="my-custom-agents",
            idempotency_key="original-custom-plan",
            tasks=(
                AgentTaskSpec(id="research", agent_id=created.id, objective="Review the brief."),
            ),
        ),
    )
    collision = AgentProfile(id=created.id, instructions="Different operator role.")
    manifest = h.platform.manifest.model_copy(
        update={
            "agents": (*h.platform.manifest.agents, collision),
            "teams": (
                *h.platform.manifest.teams,
                TeamTemplate(
                    id="operator",
                    name="Operator role",
                    agent_ids=(collision.id,),
                    allowed_workspace_ids=frozenset({uuid4()}) if hidden else frozenset(),
                ),
            ),
        }
    )
    current = AgentPlatformService(h.platform.store, manifest, state_dir=tmp_path, environ={})
    assert current.agent_profiles.get(h.actor, created.id).state == "blocked"
    assert created.id not in {profile.id for profile in current.profiles(h.actor)}
    assert created.id not in {profile["id"] for profile in current.catalog(h.actor)["agents"]}
    assert current.get(h.actor, original_plan.id) == original_plan
    # A persisted project's agent IDs cannot acquire the colliding stock profile,
    # regardless of whether the operator also made that stock role visible.
    current.project_team_resolver = lambda *_: TeamTemplate(
        id="saved-project-team",
        name="Existing project",
        agent_ids=(created.id,),
    )
    with pytest.raises(ValidationError, match="unavailable"):
        current.plan(
            h.actor,
            PlanTeamRequest(
                team_id="saved-project-team",
                project_id=uuid4(),
                idempotency_key="next-project-cycle",
                tasks=(
                    AgentTaskSpec(
                        id="research",
                        agent_id=created.id,
                        objective="Continue the saved project.",
                    ),
                ),
            ),
        )
