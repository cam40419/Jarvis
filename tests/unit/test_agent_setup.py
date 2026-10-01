import pytest

from simon.adapters.memory import InMemoryStore
from simon.agent_setup import starter_manifest
from simon.config import Settings
from simon.domain.agent_platform import PlatformManifest
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.models import ActorContext, Channel
from simon.services.agent_platform import AgentPlatformService


def test_starter_manifest_has_working_contracts_without_secret_values():
    manifest = starter_manifest(Settings(model_provider="local"))
    parsed = PlatformManifest.model_validate_json(manifest.model_dump_json())
    assert {team.id for team in parsed.teams} == {
        "documents",
        "google",
        "development",
        "production",
        "web",
        "integrations",
        "creation",
        "engineering",
        "project-studio",
        "read-only-research",
    }
    assert not parsed.models[0].enabled
    assert parsed.models[0].input_cost_per_million_usd is None
    assert not parsed.environments[0].enabled
    tools = {tool.id: tool for tool in parsed.tools}
    developer = next(agent for agent in parsed.agents if agent.id == "developer")
    assert tools["workspace.python_execute"].transport == "environment"
    assert "workspace.import_local" in developer.tool_ids
    assert "git.commit" in developer.tool_ids
    assert all(tools[tool].required_scopes <= developer.tool_scopes for tool in developer.tool_ids)
    for profile in parsed.agents:
        assert all(tools[key].required_scopes <= profile.tool_scopes for key in profile.tool_ids)


@pytest.mark.parametrize(
    ("profile_id", "required_tools", "required_scopes"),
    [
        (
            "file-writer",
            {
                "local_files_roots",
                "local_files_list",
                "local_files_search",
                "local_file_read",
                "local_file_write",
                "local_file_edit",
                "local_folder_create",
                "project_list",
            },
            {"jobs:read", "jobs:write", "memories:read"},
        ),
        (
            "drive-writer",
            {
                "google_accounts_list",
                "drive_search_files",
                "drive_read_file",
                "drive_list_folder",
                "project_list",
                "project_files_list",
                "project_file_read",
                "project_sheet_read",
                "project_file_create",
                "project_file_edit",
                "project_sheet_write",
                "project_file_rename",
            },
            {"jobs:read", "jobs:write", "threads:read", "memories:read"},
        ),
    ],
)
def test_combined_document_profiles_can_research_and_save_without_unrelated_grants(
    profile_id,
    required_tools,
    required_scopes,
):
    manifest = starter_manifest(Settings(model_provider="local"))
    tools = {tool.id: tool for tool in manifest.tools}
    profile = next(agent for agent in manifest.agents if agent.id == profile_id)

    # These complete workflows must not gain Gmail, Calendar, purchasing, shell or
    # other provider grants as the catalog grows.
    output_tools = {"project.outputs", "project.output_read"}
    if profile_id == "file-writer":
        output_tools.add("project.output_save")
    assert set(profile.tool_ids) == {f"native.{name}" for name in required_tools} | output_tools
    assert len(profile.tool_ids) == len(required_tools) + len(output_tools)
    assert profile.tool_scopes == required_scopes
    assert profile.environment_ids == ()
    assert any(not tools[key].side_effect for key in profile.tool_ids)
    assert any(tools[key].side_effect for key in profile.tool_ids)
    assert profile.max_action == "write"
    assert profile.version == 3
    assert profile.description
    assert profile.max_steps == 16
    assert profile.max_tool_calls == 32
    assert profile.max_output_tokens == 4096


def test_read_only_researchers_remain_available_with_original_grants():
    manifest = starter_manifest(Settings(model_provider="local"))
    tools = {tool.id: tool for tool in manifest.tools}
    profiles = {agent.id: agent for agent in manifest.agents}
    expected = {
        "file-reader": {
            "local_files_roots",
            "local_files_list",
            "local_files_search",
            "local_file_read",
            "project_list",
        },
        "google-reader": {
            "google_accounts_list",
            "drive_search_files",
            "drive_read_file",
            "drive_list_folder",
            "gmail_search_messages",
            "gmail_read_message",
            "calendar_list_events",
        },
    }
    for profile_id, names in expected.items():
        profile = profiles[profile_id]
        assert set(profile.tool_ids) == {f"native.{name}" for name in names}
        assert profile.max_action == "read"
        assert profile.version == 1
        assert all(not tools[key].side_effect for key in profile.tool_ids)
        assert "jobs:write" not in profile.tool_scopes
    assert profiles["file-reader"].tool_scopes == {"jobs:read", "memories:read"}
    assert profiles["google-reader"].tool_scopes == {"jobs:read", "threads:read"}


def test_default_document_teams_use_complete_deliverable_owners():
    manifest = starter_manifest(Settings(model_provider="local"))
    teams = {team.id: team for team in manifest.teams}
    expected = {
        "documents": ("file-writer", "reviewer"),
        "google": ("drive-writer", "reviewer"),
        "project-studio": (
            "project-lead",
            "business-operator",
            "file-writer",
            "drive-writer",
            "reviewer",
        ),
    }
    for team_id, agent_ids in expected.items():
        assert teams[team_id].agent_ids == agent_ids
        assert teams[team_id].version == 2

    lead = next(agent for agent in manifest.agents if agent.id == "project-lead")
    assert lead.version == 3
    assert set(lead.tool_ids) == {
        "project.snapshot",
        "project.record_finding",
        "project.add_todo",
        "project.knowledge_read",
        "project.history_search",
        "project.outputs",
        "project.output_read",
    }


def test_optional_research_team_keeps_preserved_profiles_visible_in_catalog(tmp_path):
    manifest = starter_manifest(Settings(model_provider="local"))
    platform = AgentPlatformService(
        InMemoryStore(),
        manifest,
        state_dir=tmp_path / "agents",
        environ={},
    )
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write", "memories:read", "threads:read"}),
    )
    catalog = platform.catalog(actor)
    assert {agent["id"] for agent in catalog["agents"]} == {agent.id for agent in manifest.agents}
    research = next(team for team in manifest.teams if team.id == "read-only-research")
    assert research.agent_ids == ("file-reader", "google-reader")
    assert research.version == 1
