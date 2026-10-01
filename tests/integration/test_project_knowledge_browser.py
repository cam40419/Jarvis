"""Durable project reference context and full-history retrieval in the browser."""

import pytest

from tests.integration.test_project_command_browser import create_project
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_brief_search_pin_provenance_and_conflict_keep_project_context(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, _, _, _, _ = project_ui
    project_id = create_project(page, "Camera feasibility")
    page.locator("#pc-knowledge-summary").get_by_role("button", name="Add brief").click()
    page.get_by_label("Project brief", exact=True).fill(
        "Build a camera prototype. Preserve existing connections and verify each result."
    )
    page.get_by_role("button", name="Save brief", exact=True).click()
    expect(page.locator("#pk-editor")).not_to_be_visible()
    expect(page.locator("#pc-knowledge-summary")).to_contain_text("Preserve existing connections")
    source = page.evaluate(
        """async id => {
          await api('/v1/projects/' + id + '/activity', {
            entry:{kind:'finding',text:'Cooling%_exact: use one active camera at a time.'},
            idempotency_key:'knowledge-original-finding'});
          for (let index=0; index<26; index++) await api('/v1/projects/' + id + '/activity', {
            entry:{kind:'progress',text:'Reviewed prototype step ' + index},
            idempotency_key:'knowledge-history-step-' + index});
          return (await api('/v1/projects/' + id + '/knowledge/history?kind=finding')).items[0];
        }""",
        project_id,
    )
    page.locator("#pc-tab-knowledge").click()
    expect(page.locator("#pk-history-list .pk-history-entry")).to_have_count(20)
    page.get_by_label("Search project history", exact=True).fill("Cooling")
    expect(page.get_by_role("button", name="Load earlier entries", exact=True)).to_be_disabled()
    page.get_by_label("Search project history", exact=True).fill("")
    page.get_by_role("button", name="Load earlier entries", exact=True).click()
    expect(page.locator("#pk-history-list")).to_contain_text("Cooling%_exact")
    page.get_by_label("Search project history", exact=True).fill("Cooling%_exact")
    page.get_by_role("button", name="Search history", exact=True).click()
    expect(page.locator("#pk-history-list .pk-history-entry")).to_have_count(1)
    page.get_by_role("button", name="Pin as decision", exact=True).click()
    page.get_by_label("Decision title", exact=True).fill("Use one camera at a time")
    page.get_by_label("Decision and context", exact=True).fill(
        "Start with a single active camera and verify the electrical design before fabrication."
    )
    page.get_by_role("button", name="Save decision", exact=True).click()
    expect(page.locator("#pk-editor")).not_to_be_visible()
    expect(page.locator(".pk-pinned")).to_contain_text("Use one camera at a time")
    page.get_by_role("button", name="View source entry", exact=True).click()
    expect(page.locator("#pk-source-content")).to_contain_text("Cooling%_exact")
    page.keyboard.press("Escape")
    page.reload()
    page.locator("#pc-tab-knowledge").click()
    expect(page.locator(".pk-pinned")).to_contain_text("Use one camera at a time")
    record = page.evaluate("id => api('/v1/projects/' + id + '/knowledge')", project_id)
    assert record["pinned_decisions"][0]["source_activity_id"] == source["id"]
    page.locator("#project-knowledge").get_by_role("button", name="Edit brief", exact=True).click()
    page.get_by_label("Project brief", exact=True).fill("Keep this unsaved draft.")
    page.evaluate(
        """async id => {
          const record = await api('/v1/projects/' + id + '/knowledge');
          await api('/v1/projects/' + id + '/knowledge', {
            expected_version:record.version,idempotency_key:'parallel-brief-editor',
            brief:'A newer brief from another editor.',
            pinned_decisions:record.pinned_decisions},'PATCH');
        }""",
        project_id,
    )
    page.get_by_role("button", name="Save brief", exact=True).click()
    expect(page.locator("#pk-edit-error")).to_contain_text("Your draft is preserved")
    expect(page.get_by_label("Project brief", exact=True)).to_have_value("Keep this unsaved draft.")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Refresh record", exact=True).click()
    expect(page.locator(".pk-brief")).to_contain_text("A newer brief from another editor")
    page.get_by_label("Search project history", exact=True).fill("Cooling%_exact")
    page.get_by_role("button", name="Search history", exact=True).click()
    expect(page.locator("#pk-history-list .pk-history-entry")).to_have_count(1)
    page.screenshot(path=str(tmp_path / "project-knowledge-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_label("Search project history", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "project-knowledge-mobile.png"), animations="disabled")
    page.get_by_role("button", name="Unpin", exact=True).click()
    expect(page.locator(".pk-pinned")).to_contain_text("No pinned decisions")
    expect(page.locator("#pk-history-list")).to_contain_text("Cooling%_exact")


def test_knowledge_read_access_and_old_project_responses_are_isolated(project_ui):
    from playwright.sync_api import expect

    page, _, _, _, _ = project_ui
    first = create_project(page, "First record")
    page.evaluate(
        """id => api('/v1/projects/' + id + '/knowledge', {
          expected_version:0,idempotency_key:'first-project-brief',
          brief:'First project context',pinned_decisions:[]},'PATCH')""",
        first,
    )
    page.locator("#project-back").click()
    second = create_project(page, "Second record")
    page.evaluate(
        """id => api('/v1/projects/' + id + '/knowledge', {
          expected_version:0,idempotency_key:'second-project-brief',
          brief:'Second project context',pinned_decisions:[]},'PATCH')""",
        second,
    )
    delayed = []

    def hold_first(route):
        delayed.append((route, route.fetch()))

    page.route(f"**/v1/projects/{first}/knowledge", hold_first)
    page.evaluate("id => { SimonProjectCommand.open(id); }", first)
    expect(page.locator("#pc-project-title")).to_have_text("First record")
    page.wait_for_function("document.querySelector('#pc-knowledge-summary') !== null")
    page.evaluate("id => SimonProjectCommand.open(id)", second)
    expect(page.locator("#pc-project-title")).to_have_text("Second record")
    expect(page.locator("#pc-knowledge-summary")).to_contain_text("Second project context")
    assert delayed
    for route, response in delayed:
        route.fulfill(response=response)
    expect(page.locator("#pc-knowledge-summary")).not_to_contain_text("First project context")
    page.unroute(f"**/v1/projects/{first}/knowledge", hold_first)

    def read_only_session(route):
        response = route.fetch()
        view = response.json()
        view["scopes"] = [scope for scope in view["scopes"] if scope != "jobs:write"]
        route.fulfill(response=response, json=view)

    page.route("**/auth/session", read_only_session)
    page.reload()
    page.locator("#pc-tab-knowledge").click()
    expect(page.locator("#project-knowledge")).to_contain_text(
        "Editing requires project write access"
    )
    expect(
        page.locator("#project-knowledge").get_by_role("button", name="Edit brief")
    ).to_have_count(0)
    expect(page.locator("#pc-panel-action")).not_to_be_visible()
    expect(page.locator("#project-knowledge")).to_contain_text("Second project context")
