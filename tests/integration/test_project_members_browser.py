"""Individually configured project members through real authenticated services."""

import pytest

from tests.integration.test_agent_library_browser import agent_library_ui as agent_library_ui
from tests.integration.test_project_command_browser import create_project
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def command(page, project_id):
    return page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)


def test_project_members_have_independent_skills_and_persist_without_library_changes(
    agent_library_ui, tmp_path
):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    other_project = create_project(page, "Unchanged project")
    other_before = command(page, other_project)["state"]
    library_before = page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"]
    mutations = []
    page.on(
        "request",
        lambda request: (
            mutations.append(request.url)
            if request.method in {"POST", "PATCH"} and "/v1/agent-platform/agents" in request.url
            else None
        ),
    )
    page.locator("#project-back").click()
    page.locator("#pc-new-project").click()
    project = page.locator("#pc-create-dialog")
    project.get_by_label("Project name", exact=True).fill("Independent team")
    project.get_by_label("What does success look like?", exact=True).fill("A researched report.")
    project.get_by_label("Team name", exact=True).fill("Evidence team")
    project.locator('[data-pc-member][value="writer"]').uncheck()
    editor = page.locator("#pc-member-dialog")
    for name, writing in [("Research and reports", True), ("Evidence reviewer", False)]:
        project.get_by_role("button", name="Add agent", exact=True).click()
        expect(editor).to_be_visible()
        page.locator("#pc-member-template").select_option("file-reader")
        editor.get_by_label("Role title", exact=True).fill(name)
        editor.get_by_label("Role description", exact=True).fill(
            "Research sources, write useful findings, and check the evidence."
        )
        expect(editor.get_by_role("checkbox", name="Read a local file", exact=True)).to_be_checked()
        if writing:
            editor.get_by_role("checkbox", name="Write a local file", exact=True).check()
            expect(page.locator("#pc-member-count")).to_have_text("2 selected")
            expect(editor).to_contain_text("Needs setup")
            expect(editor).not_to_contain_text("native.drive_read_file")
            page.screenshot(
                path=str(tmp_path / "project-member-desktop.png"), animations="disabled"
            )
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#sidebar")).not_to_be_in_viewport()
            assert editor.evaluate("el => el.scrollWidth <= el.clientWidth")
            page.locator("#pc-member-save").scroll_into_view_if_needed()
            page.screenshot(path=str(tmp_path / "project-member-mobile.png"), animations="disabled")
            page.set_viewport_size({"width": 1440, "height": 1080})
        editor.get_by_role("button", name="Save team member", exact=True).click()
        expect(editor).not_to_be_visible()
    expect(project.get_by_label("Project name", exact=True)).to_have_value("Independent team")
    expect(project.get_by_label("Team name", exact=True)).to_have_value("Evidence team")
    selected = project.locator("[data-pc-member]:checked")
    expect(selected).to_have_count(2)
    first, second = selected.nth(0).input_value(), selected.nth(1).input_value()
    assert first.startswith("member-") and second.startswith("member-") and first != second
    expect(project.get_by_label("Project lead", exact=True)).to_have_value(first)
    project.get_by_role("button", name="Create project", exact=True).click()
    expect(project).not_to_be_visible()
    expect(page.locator("#pc-lead-name")).to_have_text("Research and reports")
    project_id = page.url.split("project=")[1]
    before = command(page, project_id)
    assert before["state"]["team"]["members"][first]["skill_ids"] == [
        "tool.native.local_file_read",
        "tool.native.local_file_write",
    ]
    assert before["state"]["team"]["members"][second]["skill_ids"] == [
        "tool.native.local_file_read"
    ]
    page.reload()
    page.locator("#pc-tab-team").click()
    page.get_by_role("button", name="Edit skills: Research and reports", exact=True).click()
    expect(editor.get_by_label("Role title", exact=True)).to_have_value("Research and reports")
    expect(editor.locator('input[type="checkbox"]:checked')).to_have_count(2)
    editor.get_by_label("Role title", exact=True).fill("Report editor")
    editor.get_by_role("checkbox", name="Read a local file", exact=True).uncheck()
    editor.get_by_role("button", name="Save team member", exact=True).click()
    page.locator("#pc-settings-dialog").get_by_role(
        "button", name="Save settings", exact=True
    ).click()
    expect(page.locator("#pc-settings-dialog")).not_to_be_visible()
    expect(page.locator("#pc-lead-name")).to_have_text("Report editor")
    page.reload()
    after = command(page, project_id)
    assert after["state"]["team"]["agent_ids"] == [first, second]
    assert after["state"]["team"]["members"][first]["skill_ids"] == ["tool.native.local_file_write"]
    assert after["state"]["team"]["members"][second] == before["state"]["team"]["members"][second]
    assert command(page, other_project)["state"] == other_before
    assert (
        page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == library_before
    )
    assert not mutations


def test_configure_existing_member_keeps_assignment_and_cancel_discards_draft(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    project_id = create_project(page)
    page.evaluate(
        """id => api('/v1/projects/' + id + '/todos', {
          todo:{id:'assigned-task',title:'Existing assignment',
            objective:'Prepare report',agent_id:'writer'},
          idempotency_key:'member-browser-assignment'})""",
        project_id,
    )
    page.evaluate("() => SimonProjectCommand.refresh()")
    page.locator("#pc-settings").click()
    settings = page.locator("#pc-settings-dialog")
    settings.get_by_role("button", name="Configure agent: Report writer", exact=True).click()
    editor = page.locator("#pc-member-dialog")
    expect(editor.locator('input[type="checkbox"]:checked')).to_have_count(1)
    editor.get_by_label("Role title", exact=True).fill("Discard this title")
    page.keyboard.press("Escape")
    expect(editor).not_to_be_visible()
    settings.get_by_role("button", name="Configure agent: Report writer", exact=True).click()
    expect(editor.get_by_label("Role title", exact=True)).to_have_value("Report writer")
    for checkbox in editor.locator('input[type="checkbox"]:checked').all():
        checkbox.uncheck()
    expect(page.locator("#pc-member-save")).to_be_disabled()
    editor.get_by_role("checkbox", name="Read a local file", exact=True).check()
    editor.get_by_label("Role title", exact=True).fill("Project researcher")
    editor.get_by_role("button", name="Save team member", exact=True).click()
    settings.get_by_role("button", name="Save settings", exact=True).click()
    expect(settings).not_to_be_visible()
    after = command(page, project_id)
    assert after["state"]["team"]["agent_ids"] == ["writer"]
    assert after["state"]["team"]["members"]["writer"]["name"] == "Project researcher"
    assert after["state"]["todos"][0]["agent_id"] == "writer"
    page.reload()
    expect(page.locator("#pc-lead-name")).to_have_text("Project researcher")
    page.locator("#project-back").click()
    page.locator("#pc-new-project").click()
    page.locator("#pc-create-dialog").get_by_role(
        "button", name="Configure agent: Report writer", exact=True
    ).click()
    expect(editor.get_by_label("Role title", exact=True)).to_have_value("Report writer")
    expect(editor.get_by_role("checkbox", name="Read a local file", exact=True)).not_to_be_checked()
    expect(editor.get_by_role("checkbox", name="Analysis and drafting", exact=True)).to_be_checked()


def test_large_individual_skill_catalog_keeps_search_and_save_accessible(
    agent_library_ui, tmp_path
):
    from playwright.sync_api import expect

    from simon.agent_setup import starter_manifest
    from simon.services.agent_profiles import _tool_category, _tool_name

    page, container = agent_library_ui
    tools = starter_manifest(container.settings).tools

    def catalog_response(route):
        response = route.fetch()
        catalog = response.json()
        known = {skill["id"] for skill in catalog["individual_skills"]}
        catalog["individual_skills"].extend(
            {
                "id": "tool." + tool.id,
                "name": _tool_name(tool),
                "description": tool.description,
                "category": _tool_category(tool),
                "tool_ids": [tool.id],
                "source_agent_ids": [],
                "state": "unconfigured",
                "blocked_reasons": ["Configure this integration before running a task."],
            }
            for tool in tools
            if "tool." + tool.id not in known
        )
        route.fulfill(response=response, json=catalog)

    page.route("**/v1/agent-platform/catalog", catalog_response)
    page.reload()
    page.locator("#work-open").click()
    expect(page.locator("#pc-status")).to_contain_text("Saved work")
    page.locator("#pc-new-project").click()
    project = page.locator("#pc-create-dialog")
    project.get_by_role("button", name="Add agent", exact=True).click()
    editor = page.locator("#pc-member-dialog")
    editor.get_by_label("Role title", exact=True).fill("Research and documents lead")
    editor.get_by_label("Role description", exact=True).fill(
        "Find reliable sources, prepare the project documents, and verify saved results."
    )
    assert editor.locator('input[type="checkbox"]').count() >= 90
    expect(page.locator("#pc-member-save")).to_be_in_viewport()
    page.locator("#pc-member-search").fill("local file")
    read = editor.get_by_role("checkbox", name="Read a local file", exact=True)
    read.focus()
    page.keyboard.press("Space")
    expect(read).to_be_checked()
    editor.get_by_role("checkbox", name="Write a local file", exact=True).check()
    page.locator("#pc-member-search").fill("")
    expect(page.locator("#pc-member-save")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "large-member-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    assert editor.evaluate("el => el.scrollWidth <= el.clientWidth")
    expect(page.locator("#pc-member-save")).to_be_in_viewport()
    page.locator("#pc-member-search").fill("local file")
    read.scroll_into_view_if_needed()
    expect(read).to_be_in_viewport()
    expect(page.locator("#pc-member-search")).to_be_in_viewport()
    expect(page.locator("#pc-member-save")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "large-member-mobile.png"), animations="disabled")
    editor.get_by_role("button", name="Save team member", exact=True).click()
    expect(editor).not_to_be_visible()
    expect(project).to_contain_text("Research and documents lead")
