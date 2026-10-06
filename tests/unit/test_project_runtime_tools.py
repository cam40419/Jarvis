from simon.adapters.project_runtime_tools import with_project_runtime_tools
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate


def test_installed_project_tools_are_selectable_without_widening_existing_agent_grants():
    profile = AgentProfile(id="writer", instructions="Write clear documents.", tool_ids=())
    manifest = PlatformManifest(
        agents=(profile,),
        teams=(
            TeamTemplate(
                id="studio",
                name="Studio",
                agent_ids=("writer",),
            ),
        ),
    )
    enriched = with_project_runtime_tools(manifest)
    assert enriched.agents == manifest.agents and enriched.teams == manifest.teams
    assert {
        "project.journal_list",
        "project.journal_read",
        "project.records_search",
        "project.record_update",
        "clickup.connections_list",
        "clickup.boards_list",
        "clickup.account_tasks_list",
        "clickup.account_task_read",
        "clickup.shared_tasks_list",
    } <= {tool.id for tool in enriched.tools}
    assert with_project_runtime_tools(enriched) == enriched
    disabled = enriched.model_copy(
        update={
            "tools": tuple(tool.model_copy(update={"enabled": False}) for tool in enriched.tools)
        }
    )
    assert all(not tool.enabled for tool in with_project_runtime_tools(disabled).tools)


def test_empty_platform_does_not_become_configured_just_by_loading_runtime_tools():
    manifest = PlatformManifest()
    assert with_project_runtime_tools(manifest) == manifest
