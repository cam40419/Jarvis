import os

import pytest

from simon.adapters.postgres import PostgresStore
from tests.contract.test_connected import connected_setup
from tests.contract.test_local_files import zip_bytes
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_browser_upload_unzip_read_and_edit_prompt(postgres_url, tmp_path, monkeypatch):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    _, actor, token = connected_setup(PostgresStore(postgres_url))
    monkeypatch.setenv("SIMON_LOCAL_FILES_ENABLED", "true")
    monkeypatch.setenv("SIMON_LOCAL_FILES_DIR", str(tmp_path / "files"))
    with running_api(postgres_url, tmp_path / "local-files.log") as api, sync_playwright() as pw:
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context()
            origin = str(api.base_url).rstrip("/")
            context.add_cookies(
                [{"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"}]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/chat")
            expect(page.locator("#new-chat")).to_be_enabled()
            page.locator("#work-open").click()
            page.locator("#local-files-open").click()
            panel = page.locator("#local-files-panel")
            expect(panel.locator("#local-file-upload")).to_be_visible()
            panel.locator("#local-file-upload").set_input_files(
                {
                    "name": "Stdout.zip",
                    "mimeType": "application/zip",
                    "buffer": zip_bytes([("notes.md", "Extracted notes")]),
                }
            )
            expect(panel.locator("strong")).to_have_text("Stdout.zip")
            panel.get_by_role("button", name="Inspect ZIP").click()
            expect(panel.locator("pre")).to_contain_text("notes.md")
            page.once("dialog", lambda dialog: dialog.accept("imports/Stdout"))
            panel.get_by_role("button", name="Extract ZIP").click()
            expect(panel.locator("strong")).to_have_text("notes.md")
            panel.get_by_role("button", name="Read", exact=True).click()
            expect(panel.locator("pre")).to_have_text("Extracted notes")
            panel.get_by_role("button", name="Edit with Simon").click()
            expect(panel).not_to_be_visible()
            assert "imports/Stdout/notes.md" in page.locator("#text").input_value()
            assert not errors
            extracted = (
                tmp_path
                / "files"
                / str(actor.household_id)
                / str(actor.actor_id)
                / "imports/Stdout/notes.md"
            )
            assert extracted.read_text() == "Extracted notes"
        finally:
            browser.close()
