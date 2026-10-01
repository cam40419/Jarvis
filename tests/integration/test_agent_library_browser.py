"""Reusable skill-based agents through the real authenticated browser API."""

import pytest

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


@pytest.fixture
def agent_library_ui(project_ui, tmp_path):
    page, _, _, container, _ = project_ui
    original = container.agent_platform.manifest
    tools = (
        ToolDefinition(
            id="native.local_file_read",
            description="Read local files",
            transport="native",
            configured=True,
            required_scopes=frozenset({"jobs:read"}),
        ),
        ToolDefinition(
            id="native.local_file_write",
            description="Write local documents",
            transport="native",
            configured=True,
            side_effect=True,
            action_policy="write",
            required_scopes=frozenset({"jobs:write"}),
        ),
        ToolDefinition(
            id="native.drive_read_file",
            description="Read Drive files",
            transport="native",
            configured=False,
            required_scopes=frozenset({"jobs:read"}),
        ),
    )
    manifest = PlatformManifest(
        tools=tools,
        agents=(
            original.agents[0],
            AgentProfile(
                id="file-reader",
                name="File research",
                description="Read and compare local sources.",
                instructions="Research authorized files.",
                tool_ids=(tools[0].id,),
                tool_scopes=frozenset({"jobs:read"}),
            ),
            AgentProfile(
                id="file-writer",
                name="Document writing",
                description="Write and revise reports.",
                instructions="Write and check local documents.",
                tool_ids=(tools[1].id,),
                tool_scopes=frozenset({"jobs:read", "jobs:write"}),
                max_action="write",
            ),
            AgentProfile(
                id="google-reader",
                name="Drive research",
                description="Read connected Drive files.",
                instructions="Research connected Drive files.",
                tool_ids=(tools[2].id,),
                tool_scopes=frozenset({"jobs:read"}),
            ),
        ),
        teams=(
            original.teams[0],
            TeamTemplate(
                id="documents",
                name="Documents",
                agent_ids=("file-reader", "file-writer", "google-reader"),
            ),
        ),
        models=original.models,
    )
    configured = AgentPlatformService(
        container.store,
        manifest,
        state_dir=tmp_path,
        environ={},
        available_transports=("native",),
    )
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_platform.project_team_resolver = container.project_coordinator.resolve_team
    container.agent_platform.project_profile_resolver = container.project_work.member_profiles
    container.agent_platform.project_visibility_resolver = container.project_work.project_resolver
    container.project_work.role_capture = container.agent_platform.agent_profiles.capture_role
    container.project_work.role_resolver = container.agent_platform.agent_profiles.resolve_role
    page.reload()
    page.locator("#work-open").click()
    return page, container


def fill_agent(page, name="Research coordinator"):
    page.locator("#agent-editor").get_by_label("Role title", exact=True).fill(name)
    page.locator("#agent-editor").get_by_label("Role description", exact=True).fill(
        "Research the source material, write the report, and verify the saved document."
    )
    page.locator("#agent-editor").get_by_role("checkbox", name="File research", exact=True).check()
    page.locator("#agent-editor").get_by_role(
        "checkbox", name="Document writing", exact=True
    ).check()


def test_create_combined_agent_in_library_and_reuse_its_role(
    agent_library_ui,
    tmp_path,
):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    expect(page.locator("#agent-editor")).to_be_visible()
    fill_agent(page)
    expect(page.locator("#al-selected-count")).to_have_text("2 selected")
    expect(page.locator("#al-skills")).to_contain_text("Needs setup")
    expect(page.locator("#al-skills")).not_to_contain_text("native.drive_read_file")
    page.screenshot(path=str(tmp_path / "agent-editor-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    assert page.locator("#agent-editor").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=str(tmp_path / "agent-editor-mobile.png"), animations="disabled")
    page.get_by_role("button", name="Save agent", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    expect(page.locator("#al-agent-list")).to_contain_text("Research coordinator")
    expect(page.locator("#al-agent-list")).to_contain_text("File research · Document writing")
    page.get_by_role("button", name="Edit agent: Research coordinator", exact=True).click()
    expect(page.locator("#agent-editor").get_by_label("Role title", exact=True)).to_have_value(
        "Research coordinator"
    )
    expect(page.locator("#agent-editor input[type=checkbox]:checked")).to_have_count(2)
    expect(page.locator("#al-save-note")).to_contain_text("Changes apply to future plans")
    page.locator("#agent-editor").get_by_label("Role title", exact=True).fill(
        "Research & reports lead"
    )
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    expect(page.locator("#al-agent-list")).to_contain_text("Research & reports lead")
    page.reload()
    page.locator("#work-open").click()
    page.locator("#agent-tab-agents").click()
    expect(page.locator("#al-agent-list")).to_contain_text("Research & reports lead")
    page.locator("#pc-new-project").click()
    page.locator("#pc-create-dialog").get_by_role("button", name="Add agent", exact=True).click()
    expect(page.locator("#pc-member-template")).to_contain_text("Research & reports lead")


def test_agent_editor_keeps_conflicting_draft_and_marks_unconfigured_skills(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    fill_agent(page, "Cloud researcher")
    page.locator("#agent-editor").get_by_role("checkbox", name="Drive research", exact=True).check()
    expect(page.locator("#al-save-note")).to_contain_text("remain blocked")
    page.get_by_role("button", name="Save agent", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    expect(page.locator("#al-agent-list .al-badge")).to_have_text("Needs attention")
    page.get_by_role("button", name="Edit agent: Cloud researcher", exact=True).click()
    page.locator("#agent-editor").get_by_label("Role title", exact=True).fill(
        "Keep this unsaved title"
    )
    page.evaluate(
        """async () => {
          const record = (await api('/v1/agent-platform/catalog')).custom_agents[0];
          await api('/v1/agent-platform/agents/' + record.id, {name: 'Saved by another editor',
            description: record.description, skill_ids: record.skill_ids,
            expected_version: record.version, idempotency_key: 'concurrent-agent-change'}, 'PATCH');
        }"""
    )
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.locator("#al-feedback")).to_contain_text("Your draft is preserved")
    expect(page.locator("#agent-editor").get_by_label("Role title", exact=True)).to_have_value(
        "Keep this unsaved title"
    )
    expect(page.locator("#agent-editor input[type=checkbox]:checked")).to_have_count(3)
    page.keyboard.press("Escape")
    page.locator("#agent-refresh").click()
    expect(page.locator("#al-agent-list")).to_contain_text("Saved by another editor")
    page.locator("#al-new-agent").click()
    expect(page.get_by_role("button", name="Save agent", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
