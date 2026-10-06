"""Find and grant separate Calendar/ClickUp skills without connected accounts."""

import pytest

from simon.agent_setup import starter_manifest
from simon.services.agent_platform import AgentPlatformService
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


@pytest.fixture
def integration_skills_ui(project_ui, tmp_path):
    page, _, _, container, _ = project_ui

    def availability(_actor, identifier):
        if identifier.startswith("clickup."):
            return {
                "available": False,
                "reason": ("Connect your ClickUp account in Connections. "),
            }
        if (
            identifier.startswith("native.calendar_")
            and identifier != "native.calendar_action_status"
        ):
            return {"available": False, "reason": "Connect a Google account"}
        return {"available": True, "reason": ""}

    configured = AgentPlatformService(
        container.store,
        starter_manifest(container.settings),
        state_dir=tmp_path / "integrations",
        environ={},
        available_transports=("native", "project_boards", "http", "environment"),
        tool_availability=availability,
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


def test_reusable_agent_discovers_calendar_and_clickup_independently(
    integration_skills_ui, tmp_path
):
    from playwright.sync_api import expect

    page, _ = integration_skills_ui
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    editor = page.locator("#agent-editor")
    editor.get_by_label("Role title", exact=True).fill("Schedule and delivery assistant")
    editor.get_by_label("Role description", exact=True).fill(
        "Compare scheduled events with the project's ClickUp tasks. Read only."
    )
    # Exercise the full starter catalog: the save action must not sit below 100+ rows.
    assert editor.get_by_role("checkbox").count() >= 90
    expect(page.locator("#al-save")).to_be_in_viewport()
    expect(editor.get_by_label("Skill category", exact=True)).to_contain_text("Calendar")
    expect(editor.get_by_label("Skill category", exact=True)).to_contain_text("ClickUp")
    editor.get_by_label("Skill category", exact=True).select_option("Calendar")
    expect(editor.get_by_role("checkbox")).to_have_count(3)
    read_calendar = editor.get_by_role("checkbox", name="Read calendar events", exact=True)
    read_calendar.focus()
    page.keyboard.press("Space")
    expect(read_calendar).to_be_checked()
    expect(
        editor.get_by_role("checkbox", name="Create a calendar event", exact=True)
    ).not_to_be_checked()
    expect(editor).to_contain_text("Connect a Google account")
    expect(page.locator("#al-selected-count")).to_have_text("1 selected")
    page.screenshot(path=str(tmp_path / "calendar-skills-desktop.png"), animations="disabled")
    editor.get_by_label("Skill category", exact=True).select_option("ClickUp")
    expect(editor.get_by_role("checkbox")).to_have_count(13)
    editor.get_by_role("checkbox", name="List ClickUp tasks", exact=True).check()
    expect(editor).to_contain_text("Connect your ClickUp account in Connections")
    expect(
        editor.get_by_role("checkbox", name="Publish project tasks to ClickUp", exact=True)
    ).not_to_be_checked()
    expect(page.locator("#al-selected-count")).to_have_text("2 selected")
    editor.get_by_label("Skill category", exact=True).select_option("")
    editor.get_by_label("Find a skill", exact=True).fill("clickup")
    expect(editor.get_by_role("checkbox")).to_have_count(13)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    editor.get_by_role(
        "checkbox", name="List ClickUp tasks", exact=True
    ).scroll_into_view_if_needed()
    expect(page.locator("#al-skill-search")).to_be_in_viewport()
    expect(page.locator("#al-save")).to_be_in_viewport()
    assert editor.evaluate("element => element.scrollWidth <= element.clientWidth")
    page.screenshot(path=str(tmp_path / "clickup-skills-mobile.png"), animations="disabled")
    editor.get_by_role("button", name="Save agent", exact=True).click()
    expect(editor).not_to_be_visible()
    expect(page.locator("#al-agent-list .al-badge")).to_have_text("Needs attention")
    catalog = page.evaluate("() => api('/v1/agent-platform/catalog')")
    saved = catalog["custom_agents"][0]
    assert saved["skill_ids"] == ["tool.clickup.tasks_list", "tool.native.calendar_list_events"]
    assert set(saved["profile"]["tool_ids"]) == {
        "clickup.tasks_list",
        "native.calendar_list_events",
    }
    page.reload()
    page.locator("#work-open").click()
    page.locator("#agent-tab-agents").click()
    page.get_by_role(
        "button", name="Edit agent: Schedule and delivery assistant", exact=True
    ).click()
    expect(editor.locator('input[type="checkbox"]:checked')).to_have_count(2)
    editor.get_by_label("Skill category", exact=True).select_option("Calendar")
    expect(read_calendar).to_be_checked()
    editor.get_by_label("Skill category", exact=True).select_option("ClickUp")
    expect(editor.get_by_role("checkbox", name="List ClickUp tasks", exact=True)).to_be_checked()


def test_project_member_calendar_and_clickup_selections_are_project_local(
    integration_skills_ui, tmp_path
):
    from playwright.sync_api import expect

    page, _ = integration_skills_ui
    library_before = page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"]
    page.locator("#pc-new-project").click()
    project = page.locator("#pc-create-dialog")
    project.get_by_label("Project name", exact=True).fill("Delivery planning")
    project.get_by_label("What does success look like?", exact=True).fill("Review project timing.")
    while project.locator("[data-pc-member]:checked").count():
        project.locator("[data-pc-member]:checked").first.uncheck()
    project.get_by_role("button", name="Add agent", exact=True).click()
    editor = page.locator("#pc-member-dialog")
    editor.get_by_label("Role title", exact=True).fill("Delivery planner")
    editor.get_by_label("Role description", exact=True).fill(
        "Read events and project tasks without changing them."
    )
    editor.get_by_label("Skill category", exact=True).select_option("Calendar")
    expect(editor.get_by_role("checkbox")).to_have_count(3)
    editor.get_by_role("checkbox", name="Read calendar events", exact=True).check()
    expect(editor).to_contain_text("Connect a Google account")
    editor.get_by_label("Skill category", exact=True).select_option("ClickUp")
    expect(editor.get_by_role("checkbox")).to_have_count(13)
    editor.get_by_role("checkbox", name="Read a ClickUp task", exact=True).check()
    expect(editor).to_contain_text("Connect your ClickUp account in Connections")
    expect(page.locator("#pc-member-count")).to_have_text("2 selected")
    page.screenshot(path=str(tmp_path / "project-clickup-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    editor.get_by_role(
        "checkbox", name="Read a ClickUp task", exact=True
    ).scroll_into_view_if_needed()
    expect(page.locator("#pc-member-category")).to_be_in_viewport()
    expect(page.locator("#pc-member-save")).to_be_in_viewport()
    assert editor.evaluate("element => element.scrollWidth <= element.clientWidth")
    page.screenshot(path=str(tmp_path / "project-clickup-mobile.png"), animations="disabled")
    editor.get_by_role("button", name="Save team member", exact=True).click()
    expect(editor).not_to_be_visible()
    member_id = project.locator("[data-pc-member]:checked").input_value()
    project.get_by_role("button", name="Create project", exact=True).click()
    expect(project).not_to_be_visible()
    expect(page.locator("#pc-lead-name")).to_have_text("Delivery planner")
    project_id = page.url.split("project=")[1]
    state = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    assert state["team"]["members"][member_id]["skill_ids"] == [
        "tool.clickup.task_read",
        "tool.native.calendar_list_events",
    ]
    page.reload()
    page.get_by_label("Project section", exact=True).select_option("team")
    page.get_by_role("button", name="Edit skills: Delivery planner", exact=True).click()
    expect(editor.locator('input[type="checkbox"]:checked')).to_have_count(2)
    editor.get_by_label("Skill category", exact=True).select_option("Calendar")
    expect(editor.get_by_role("checkbox", name="Read calendar events", exact=True)).to_be_checked()
    expect(
        editor.get_by_role("checkbox", name="Create a calendar event", exact=True)
    ).not_to_be_checked()
    assert (
        page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == library_before
    )
