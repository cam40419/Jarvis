"""Project-local member skills survive restart without changing other teams or templates."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_profiles import AgentRoleDefinition
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel
from simon.domain.project_work import ConfigureProjectWork, ProjectTeam, ProjectTodo
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_work import ProjectWorkService

RESEARCH_ID = "member-" + "a" * 32
REPORT_ID = "member-" + "b" * 32


@pytest.fixture
def member_projects(store, tmp_path):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    first, second = uuid4(), uuid4()
    manifest = PlatformManifest(
        agents=(
            AgentProfile(
                id="editor",
                name="Editor",
                instructions="Use authorized sources.",
                tool_ids=("native.local_file_read", "native.local_file_write"),
                tool_scopes=actor.scopes,
                max_action="write",
            ),
        ),
        teams=(TeamTemplate(id="studio", name="Studio", agent_ids=("editor",)),),
        tools=(
            ToolDefinition(
                id="native.local_file_read",
                description="Read local files",
                transport="native",
                configured=True,
                required_scopes=frozenset({"jobs:read"}),
            ),
            ToolDefinition(
                id="native.local_file_write",
                description="Write local files",
                transport="native",
                configured=True,
                side_effect=True,
                action_policy="write",
                required_scopes=frozenset({"jobs:write"}),
            ),
        ),
        models=(
            ModelEndpoint(
                id="local",
                provider="openai_compatible",
                model="test-local",
                tier="economy",
                local=True,
                capabilities=frozenset({"text", "tools"}),
                base_url="http://localhost:11434/v1",
            ),
        ),
    )

    def resolve(current, project_id):
        if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id) or (
            project_id not in (first, second)
        ):
            raise NotFoundError("Project not found")
        return SimpleNamespace(id=project_id, subject="Test project", content="Test project work")

    def instance():
        from simon.adapters.postgres import PostgresStore

        backing = PostgresStore(store._database_url) if isinstance(store, PostgresStore) else store
        platform = AgentPlatformService(
            backing, manifest, state_dir=tmp_path, environ={}, available_transports=("native",)
        )
        work = ProjectWorkService(
            backing, project_resolver=resolve, actor_resolver=lambda *_: actor
        )
        runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: actor)
        coordinator = ProjectCoordinator(work, runs)
        work.team_validator = coordinator.validate_team
        return SimpleNamespace(platform=platform, work=work, coordinator=coordinator)

    api, worker = instance(), instance()
    skills = {
        item["tool_ids"][0]: item["id"]
        for item in api.platform.catalog(actor)["individual_skills"]
        if item["tool_ids"]
    }
    reader = AgentRoleDefinition(
        name="Source researcher",
        description="Read and compare sources.",
        skill_ids=(skills["native.local_file_read"],),
    )
    writer = AgentRoleDefinition(
        name="Report writer",
        description="Write the finished report.",
        skill_ids=(skills["native.local_file_write"],),
    )
    team = ProjectTeam(
        name="Project team",
        agent_ids=(RESEARCH_ID, REPORT_ID),
        lead_agent_id=RESEARCH_ID,
        members={RESEARCH_ID: reader, REPORT_ID: writer},
    )
    return SimpleNamespace(
        actor=actor,
        first=first,
        second=second,
        api=api,
        worker=worker,
        team=team,
        reader=reader,
        writer=writer,
        instance=instance,
    )


def test_member_skills_are_independent_durable_and_project_scoped(member_projects):
    h = member_projects
    before = h.api.platform.catalog(h.actor)["agents"]
    state = h.api.work.configure(
        h.actor,
        h.first,
        ConfigureProjectWork(
            expected_version=0,
            team=h.team,
        ),
    )
    h.api.work.configure(h.actor, h.second, ConfigureProjectWork(expected_version=0, team=h.team))
    assert h.worker.work.get(h.actor, h.first).team == state.team
    original = {p.id: p for p in h.worker.platform.profiles(h.actor, h.first)}
    assert original[RESEARCH_ID].tool_ids == ("native.local_file_read",)
    assert original[REPORT_ID].tool_ids == ("native.local_file_write",)
    assert original[RESEARCH_ID].max_action == "read"
    assert original[REPORT_ID].max_action == "write"

    combined = h.reader.model_copy(
        update={
            "name": "Research and reports",
            "skill_ids": (*h.reader.skill_ids, *h.writer.skill_ids),
        }
    )
    changed = h.team.model_copy(update={"members": {**h.team.members, RESEARCH_ID: combined}})
    edited = h.api.work.configure(
        h.actor,
        h.first,
        ConfigureProjectWork(
            expected_version=state.version,
            team=changed,
        ),
    )
    h.api.work.add_todo(
        h.actor,
        h.first,
        ProjectTodo(title="Review notes", objective="Review notes."),
        idempotency_key="member-snapshot-survives-todo",
    )
    restarted = h.instance()
    current = {p.id: p for p in restarted.platform.profiles(h.actor, h.first)}
    other = {p.id: p for p in h.worker.platform.profiles(h.actor, h.second)}
    assert set(current[RESEARCH_ID].tool_ids) == {
        "native.local_file_read",
        "native.local_file_write",
    }
    assert current[RESEARCH_ID].name == "Research and reports"
    assert current[REPORT_ID] == original[REPORT_ID]
    assert other[RESEARCH_ID] == original[RESEARCH_ID]
    assert h.api.platform.catalog(h.actor)["agents"] == before
    assert h.api.platform.agent_profiles.list(h.actor) == ()
    assert restarted.work.get(h.actor, h.first).team.revision == edited.team.revision
    assert {p.id for p in h.api.platform.profiles(h.actor)} == {"editor"}


def test_planner_cannot_borrow_a_teammates_or_other_projects_skill(member_projects):
    h = member_projects
    h.api.work.configure(h.actor, h.first, ConfigureProjectWork(expected_version=0, team=h.team))
    readonly_team = h.team.model_copy(
        update={
            "members": {
                RESEARCH_ID: h.reader,
                REPORT_ID: h.reader,
            }
        }
    )
    h.api.work.configure(
        h.actor,
        h.second,
        ConfigureProjectWork(
            expected_version=0,
            team=readonly_team,
        ),
    )
    for project_id, member in ((h.first, RESEARCH_ID), (h.second, REPORT_ID)):
        with pytest.raises(AuthorizationError, match="grant"):
            h.worker.platform.plan(
                h.actor,
                PlanTeamRequest(
                    team_id="project-" + project_id.hex,
                    project_id=project_id,
                    idempotency_key="borrow-grant-" + project_id.hex,
                    tasks=(
                        AgentTaskSpec(
                            id="write",
                            agent_id=member,
                            objective="Save a report.",
                            tool_ids=("native.local_file_write",),
                        ),
                    ),
                ),
            )
    valid = h.worker.platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id="project-" + h.first.hex,
            project_id=h.first,
            idempotency_key="member-authorized-plan",
            tasks=(AgentTaskSpec(id="write", agent_id=REPORT_ID, objective="Save a report."),),
        ),
    )
    assert valid.state == "planned"
    assert valid.tasks[0].tool_ids == ("native.local_file_write",)
    stranger = h.actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(NotFoundError):
        h.worker.platform.profiles(stranger, h.first)
