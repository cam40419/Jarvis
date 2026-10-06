import os

import pytest

from simon.adapters.postgres import PostgresStore
from tests.contract.test_connected import connected_setup
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_work_project_creation_browse_read_upload_and_edit_prompt(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    _, _, token = connected_setup(PostgresStore(postgres_url))
    with running_api(postgres_url, tmp_path / "project-files.log") as api, sync_playwright() as pw:
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context()
            origin = str(api.base_url).rstrip("/")
            context.add_cookies(
                [
                    {
                        "name": "simon_session",
                        "value": token,
                        "url": origin,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/chat")
            expect(page.locator("#new-chat")).to_be_enabled()
            page.locator("#work-open").click()
            page.locator("#work-background-open").click()
            page.locator("#work-project-name").fill("Browser project")
            page.locator("#work-project-detail").fill("Test live project files")
            page.locator('#work-project-form button[type="submit"]').click()
            expect(page.locator("#work-projects")).to_contain_text("Browser project")
            expect(page.locator("#work-projects")).to_contain_text("Reconnect Google")
            uploads = []
            trashes = []

            def files(route):
                route.fulfill(
                    json={
                        "folder": {"id": "folder", "name": "Project folder"},
                        "files": []
                        if trashes
                        else [
                            {
                                "id": "notes",
                                "name": "notes.md",
                                "version": "1",
                                "capabilities": {"canTrash": True},
                                "mimeType": "text/markdown",
                                "url": "https://drive.google.com/file/d/notes/view",
                            }
                        ],
                        "next_page_token": "",
                    }
                )

            page.route("**/v1/projects/*/files?*", files)
            page.route(
                "**/v1/projects/*/files/notes",
                lambda route: route.fulfill(
                    json={"text": "Live content from Drive", "revision": "rev"}
                ),
            )

            def upload(route):
                uploads.append(route.request.post_data_json)
                route.fulfill(json={"status": "succeeded", "file_id": "uploaded"})

            page.route("**/v1/projects/*/upload", upload)
            page.get_by_role("button", name="Browse files", exact=True).click()
            panel = page.locator("#project-files-panel")
            expect(panel.get_by_role("link", name="notes.md")).to_have_attribute(
                "href", "https://drive.google.com/file/d/notes/view"
            )
            panel.get_by_role("button", name="Read here").click()
            expect(panel.locator("pre")).to_have_text("Live content from Drive")
            panel.locator('input[type="file"]').set_input_files(
                {
                    "name": "upload.txt",
                    "mimeType": "text/plain",
                    "buffer": b"uploaded content",
                }
            )
            expect(panel.get_by_role("status")).to_contain_text("Live files from Drive")
            assert uploads[0]["content_base64"] == "dXBsb2FkZWQgY29udGVudA=="
            panel.get_by_role("button", name="Edit with Simon").click()
            expect(panel).not_to_be_visible()
            assert 'Edit "notes.md"' in page.locator("#text").input_value()
            assert "file ID notes" in page.locator("#text").input_value()

            def trash(route):
                trashes.append(route.request.post_data_json)
                route.fulfill(json={"status": "succeeded", "result": {"trashed": True}})

            page.route("**/v1/projects/trash-drive-item", trash)
            page.locator("#work-open").click()
            page.get_by_role("button", name="Browse files", exact=True).click()
            panel.get_by_role("button", name="Move to trash", exact=True).click()
            expect(panel).to_contain_text("This folder is empty")
            assert trashes[0]["file_id"] == "notes" and trashes[0]["revision"] == "1"
            assert not errors
        finally:
            browser.close()
