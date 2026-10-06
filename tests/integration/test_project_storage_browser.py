"""Choose multiple file roots, save and reload them from the project Files screen."""

import pytest

from tests.integration.test_project_command_browser import create_project
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_project_file_locations_save_multiple_subfolders_and_survive_reload(project_ui):
    from playwright.sync_api import expect

    page, _, _, _container, _ = project_ui
    project_id = create_project(page, "Storage locations")
    page.get_by_role("tab", name="Files", exact=True).click()
    section = page.locator("#pc-storage-locations")
    expect(section).to_be_visible()
    expect(section.get_by_role("button", name="Save file locations")).to_be_visible()
    page.evaluate(
        "async (id) => { for (const path of ['References', 'Outputs']) "
        "await api('/v1/local-files/action', {name: 'local_folder_create', "
        "arguments: {root: 'project:' + id, path}, idempotency_key: 'folder-' + path}); }",
        project_id,
    )
    section.get_by_label("Location name", exact=True).fill("Project references")
    section.get_by_label("Storage subfolder").fill("References")
    section.get_by_role("button", name="Add location").click()
    section.get_by_label("Location name", exact=True).fill("Project outputs")
    section.get_by_label("Storage subfolder").fill("Outputs")
    section.get_by_role("button", name="Add location").click()
    section.get_by_role("button", name="Remove Project files", exact=True).click()
    section.get_by_role("button", name="Save file locations").click()
    expect(section).to_contain_text("File locations saved")
    saved = page.evaluate(
        "(id) => api('/v1/projects/' + id + '/workspace/file-locations')", project_id
    )
    assert [row["path"] for row in saved["locations"]] == ["References", "Outputs"]
    page.reload()
    page.get_by_role("button", name="Work", exact=True).click()
    page.locator("#pc-project-list button").filter(has_text="Storage locations").click()
    page.get_by_role("tab", name="Files", exact=True).click()
    expect(section).to_contain_text("Project references")
    expect(section).to_contain_text("Project outputs")
    section.get_by_role("button", name="Browse Project references", exact=True).click()
    expect(section).to_contain_text("This folder is empty")
