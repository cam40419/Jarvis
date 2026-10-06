import os

import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.project_files import DriveBrowse, ProjectBind
from tests.contract.test_drive_management import setup_management
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_folder_picker_search_root_link_and_unlink(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    service, actor, files, project = setup_management(PostgresStore(postgres_url))
    token, _ = service.identity.development_login(
        service.settings.dev_login_token.get_secret_value(), None
    )
    with running_api(postgres_url, tmp_path / "drive-picker.log") as api, sync_playwright() as pw:
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
            browses, links = [], []

            def browse(route):
                values = route.request.post_data_json
                browses.append(values)
                route.fulfill(json=files.browse(actor, DriveBrowse(**values), lambda: actor))

            def link(route):
                values = route.request.post_data_json
                links.append(values)
                route.fulfill(json=files.bind(actor, ProjectBind(**values)))

            page.route("**/v1/projects/drive/browse", browse)
            page.route("**/v1/projects/link-drive", link)
            page.goto(origin + "/chat")
            expect(page.locator("#new-chat")).to_be_enabled()
            page.locator("#work-open").click()
            page.locator("#work-background-open").click()
            page.get_by_role("button", name="Link existing folder").click()
            panel = page.locator("#project-files-panel")
            expect(panel).to_contain_text("Simon - Build")
            panel.get_by_role("searchbox", name="Find Drive folder").fill("Simon")
            panel.get_by_role("button", name="Find folders").click()
            expect(panel).to_contain_text("Matching folders")
            assert browses[-1]["search_all"] and browses[-1]["query"] == "Simon"
            panel.get_by_role("button", name="Use My Drive", exact=True).click()
            expect(panel).not_to_be_visible()
            assert links[-1]["folder_id"] == "root"
            assert files.binding(actor, project).folder_id == "my-drive"
            page.get_by_role("button", name="Unlink Drive", exact=True).click()
            expect(page.locator("#work-projects")).to_contain_text("Drive unlinked")
            assert not files.binding(actor, project).enabled
            assert not errors
        finally:
            browser.close()
