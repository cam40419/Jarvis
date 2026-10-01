"""Reuse real saved agent outputs through local project storage."""

from pathlib import Path

import pytest

from tests.integration.test_project_command_browser import create_project, refresh
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_saved_output_download_reuse_and_conflict_are_visible(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui
    project_id = create_project(page, "Reusable project results")
    page.locator("#pc-command").fill("Prepare a sourced project report.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    page.locator("#pc-command").fill("Expand the report with a cost comparison.")
    page.locator("#pc-tab-files").click()
    listing = page.evaluate("id => api('/v1/projects/' + id + '/outputs')", project_id)
    brief = next(item for item in listing["items"] if item["task_id"] == "brief")
    research = next(item for item in listing["items"] if item["task_id"] == "research")
    row = page.locator(f'.po-output[data-output="{brief["id"]}"]')
    expect(row).to_be_visible()
    row.get_by_role("button", name="Save to project: answer.txt", exact=True).click()
    expect(row).to_contain_text("Saved project copy")
    with page.expect_download() as copied:
        row.get_by_role("link", name="Download project copy", exact=True).click()
    with page.expect_download() as original:
        row.get_by_role("link", name="Download original", exact=True).click()
    assert Path(copied.value.path()).read_bytes() == Path(original.value.path()).read_bytes()
    assert b"saved launch brief" in Path(copied.value.path()).read_bytes()
    row.get_by_role("button", name="Use in next task", exact=True).click()
    draft = page.locator("#pc-command").input_value()
    assert draft.startswith("Expand the report with a cost comparison.")
    assert "outputs/" in draft
    expect(page.locator("#pc-command")).to_be_focused()
    assert (
        page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"][
            "active_cycle"
        ]
        is None
    )
    page.reload()
    page.locator("#pc-tab-files").click()
    expect(row).to_contain_text("Saved project copy")
    row.get_by_role("button", name="Open local folder", exact=True).click()
    expect(page.locator("#local-files-panel")).to_contain_text(brief["id"][:8] + "-answer.txt")
    page.keyboard.press("Escape")
    destination = "outputs/" + research["id"][:8] + "-answer.txt"
    page.evaluate(
        """({id,path}) => api('/v1/local-files/action', {
          name:'local_file_write',arguments:{root:'project:'+id,path,
            content:'Human-edited file must remain intact'},
          idempotency_key:'output-conflict-file'})""",
        {"id": project_id, "path": destination},
    )
    conflict = page.locator(f'.po-output[data-output="{research["id"]}"]')
    conflict.get_by_role("button", name="Save to project: answer.txt", exact=True).click()
    expect(conflict).to_contain_text("Existing files are kept")
    with page.expect_download() as retained:
        page.evaluate(
            """({id,path}) => { const a=document.createElement('a');
              const query=new URLSearchParams({root:'project:'+id,path});
              a.href=appPath('/v1/local-files/download?'+query);
              a.download='existing.txt'; document.body.append(a); a.click(); a.remove(); }""",
            {"id": project_id, "path": destination},
        )
    assert Path(retained.value.path()).read_text() == "Human-edited file must remain intact"
    page.locator("#project-outputs").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "project-outputs-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    row.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "project-outputs-mobile.png"), animations="disabled")


def test_output_list_error_can_be_retried_and_read_only_copy_controls_are_hidden(project_ui):
    from playwright.sync_api import expect

    page, _, _, _, _ = project_ui
    project_id = create_project(page)
    endpoint = f"**/v1/projects/{project_id}/outputs?*"

    def unavailable(route):
        route.fulfill(
            status=503, json={"error": {"message": "Saved outputs are temporarily unavailable."}}
        )

    page.route(endpoint, unavailable)
    page.locator("#pc-tab-files").click()
    expect(page.locator("#po-status")).to_contain_text("temporarily unavailable")
    page.unroute(endpoint, unavailable)
    page.get_by_role("button", name="Refresh outputs", exact=True).click()
    expect(page.locator("#po-list")).to_contain_text("No generated files yet")
    page.evaluate("() => { session.scopes=session.scopes.filter(scope=>scope!=='jobs:write'); }")
    page.get_by_role("button", name="Refresh outputs", exact=True).click()
    expect(page.locator("#po-status")).to_contain_text("requires project write access")
    expect(
        page.locator("#project-outputs").get_by_role("button", name="Save to project")
    ).to_have_count(0)
