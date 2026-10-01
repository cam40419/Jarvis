import os

import pytest

from simon.adapters.postgres import PostgresStore
from tests.contract.test_connected import connected_setup
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_send_clears_immediately_and_preserves_followup_and_failed_drafts(
    postgres_url, tmp_path, monkeypatch,
):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    _, _, token = connected_setup(PostgresStore(postgres_url))
    monkeypatch.setenv("SIMON_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SIMON_GOOGLE_CLIENT_SECRET", "test-secret")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SIMON_GOOGLE_TOKEN_KEY", Fernet.generate_key().decode())
    with running_api(postgres_url, tmp_path / "composer.log") as api, sync_playwright() as pw:
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context()
            origin = str(api.base_url).rstrip("/")
            context.add_cookies([{
                "name": "simon_session", "value": token, "url": origin, "sameSite": "Strict",
            }])
            page = context.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/assistant", lambda route: route.fulfill(
                json={"provider": "openai", "auto_deep_enabled": True},
            ))
            page.route("**/runs/stream", lambda route: requests.append(route))
            page.goto(origin + "/chat")
            expect(page.locator("#new-chat")).to_be_enabled()
            page.locator("#text").fill("First message")
            page.locator("#text").press("Shift+Enter")
            expect(page.locator("#text")).to_have_value("First message\n")
            page.locator("#text").press("Enter")
            expect(page.locator("#text")).to_have_value("")
            expect(page.locator(".message.draft")).to_have_count(2)
            page.locator("#text").fill("My next draft")
            requests[-1].fulfill(
                content_type="text/event-stream",
                body='event: run.completed\ndata: {"id":"synthetic"}\n\n',
            )
            expect(page.locator("#status")).to_have_text("Run complete. Messages saved.")
            expect(page.locator("#text")).to_have_value("My next draft")
            page.locator("#text").press("Enter")
            expect(page.locator("#text")).to_have_value("")
            expect(page.locator(".message.draft")).to_have_count(2)
            page.locator("#text").fill("Keep newer text")
            requests[-1].fulfill(status=503, json={"error": {"message": "Synthetic failure"}})
            expect(page.locator("#stop")).to_be_hidden()
            expect(page.locator("#text")).to_have_value("Keep newer text")
            page.locator("#text").press("Enter")
            expect(page.locator("#text")).to_have_value("")
            expect(page.locator(".message.draft")).to_have_count(2)
            requests[-1].fulfill(status=503, json={"error": {"message": "Synthetic failure"}})
            expect(page.locator("#stop")).to_be_hidden()
            expect(page.locator("#text")).to_have_value("Keep newer text")
            page.locator("#text").press("Enter")
            expect(page.locator("#text")).to_have_value("")
            expect(page.locator(".message.draft")).to_have_count(2)
            page.locator("#stop").click()
            expect(page.locator("#status")).to_contain_text("Stopped.")
            expect(page.locator("#text")).to_have_value("Keep newer text")
            expect(page.locator("#stop")).to_be_hidden()
            # The connection panel exposes the upgrade needed by an existing account.
            page.locator("#connections-open").click()
            expect(page.locator("#google-accounts")).to_contain_text(
                "Reconnect this account to grant missing Google permissions."
            )
            expect(page.get_by_role("button", name="Reconnect", exact=True)).to_be_enabled()
            expect(page.locator("#google-accounts")).to_contain_text("Send email")
            assert "Read Gmail" not in page.locator("#google-accounts").inner_text()
            assert not errors
        finally:
            browser.close()
