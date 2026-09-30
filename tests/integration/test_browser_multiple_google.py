import os

import pytest

from simon.adapters.postgres import PostgresStore
from tests.contract.test_connected import connected_setup
from tests.contract.test_multiple_google_accounts import add
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_accounts_ui_default_disconnect_and_oauth_picker(postgres_url, tmp_path, monkeypatch):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    service, actor, token = connected_setup(PostgresStore(postgres_url))
    add(service, actor)
    monkeypatch.setenv("SIMON_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SIMON_GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv(
        "SIMON_GOOGLE_TOKEN_KEY", service.settings.google_token_key.get_secret_value()
    )
    with running_api(postgres_url, tmp_path / "accounts.log") as api, sync_playwright() as pw:
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
            page.locator("#connections-open").click()
            accounts = page.locator("#google-accounts")
            expect(accounts).to_contain_text("owner@example.com (default)")
            expect(accounts).to_contain_text("second@example.com")
            accounts.get_by_role("button", name="Make default").click()
            expect(accounts).to_contain_text("second@example.com (default)")
            owner = accounts.locator("section").filter(has_text="owner@example.com")
            owner.get_by_role("button", name="Disconnect", exact=True).click()
            expect(accounts).not_to_contain_text("owner@example.com")
            expect(accounts).to_contain_text("second@example.com (default)")
            # Intercept only the external Google page; the OAuth start uses the real API.
            page.route(
                "https://accounts.google.com/**",
                lambda route: route.fulfill(body="Choose an account"),
            )
            page.locator("#google-sharing").check()
            page.get_by_role("button", name="Add another Google account").click()
            page.wait_for_url("https://accounts.google.com/**")
            assert "select_account" in page.url
            assert not errors
        finally:
            browser.close()
