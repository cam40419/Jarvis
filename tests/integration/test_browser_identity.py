import os

import pytest

from jarvis.config import Settings
from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from jarvis.services.identity import IdentityService
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_browser_passkey_enrollment_login_logout_and_echo(postgres_url, tmp_path):
    if os.environ.get("JARVIS_BROWSER_TESTS") != "1":
        pytest.skip("set JARVIS_BROWSER_TESTS=1 to run the real browser ceremony")
    from playwright.sync_api import expect, sync_playwright

    from jarvis.adapters.postgres import PostgresStore
    from jarvis.seed import seed_development_identity

    seed_development_identity(postgres_url)
    store = PostgresStore(postgres_url)
    invitation = IdentityService(store, Settings()).enroll(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    with (
        running_api(
            postgres_url, tmp_path / "browser-api.log", dev_login=False, hostname="localhost"
        ) as api,
        sync_playwright() as playwright,
    ):
        options = {"headless": True}
        if os.environ.get("JARVIS_BROWSER_CHANNEL"):
            options["channel"] = os.environ["JARVIS_BROWSER_CHANNEL"]
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(viewport={"width": 1100, "height": 900})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            cdp = context.new_cdp_session(page)
            cdp.send("WebAuthn.enable")
            cdp.send(
                "WebAuthn.addVirtualAuthenticator",
                {
                    "options": {
                        "protocol": "ctap2",
                        "transport": "internal",
                        "hasResidentKey": True,
                        "hasUserVerification": True,
                        "isUserVerified": True,
                        "automaticPresenceSimulation": True,
                    }
                },
            )
            page.goto(str(api.base_url).rstrip("/") + "/login")
            expect(page.locator("#dev-panel")).to_be_hidden()
            page.get_by_text("Set up a passkey", exact=True).click()
            page.locator("#enrollment").fill(invitation)
            page.get_by_role("button", name="Create a passkey", exact=True).click()
            expect(page.locator("#signed-in")).to_be_visible(timeout=15000)
            expect(page.locator("#account")).to_contain_text("a passkey")
            page.get_by_role("button", name="Test connection").click()
            expect(page.locator("#status")).to_have_text("Jarvis is connected.")
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.locator("#signed-out")).to_be_visible()
            page.get_by_role("button", name="Sign in with a passkey").click()
            expect(page.locator("#signed-in")).to_be_visible(timeout=15000)
            page.reload()
            expect(page.locator("#signed-in")).to_be_visible()
            assert len(store.passkeys(DEV_ACTOR_ID)) == 1
            page.get_by_role("link", name="Open conversations").click()
            page.locator("#title").fill("Browser conversation")
            page.get_by_role("button", name="Create conversation", exact=True).click()
            expect(page.locator("#status")).to_have_text("Conversation created.")
            page.get_by_text("Shared household memories", exact=True).click()
            page.locator("#memory-subject").fill("Dinner preference")
            page.locator("#memory-content").fill("Vegetarian dinners")
            page.get_by_role("button", name="Save shared memory", exact=True).click()
            expect(page.locator("#status")).to_have_text("Shared memory saved.")
            page.locator("#text").fill("<script>untrusted text</script>")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("#status")).to_have_text("Run complete. Messages saved.")
            expect(page.locator("#messages p")).to_have_count(2)
            page.get_by_text("Last run context", exact=True).click()
            expect(page.locator("#context-details")).to_contain_text("Vegetarian dinners")
            expect(page.locator("#context-details")).to_contain_text("context-v1")
            page.get_by_role("button", name="Retract", exact=True).click()
            expect(page.locator("#memories p")).to_have_count(0)
            expect(page.locator("#messages")).to_contain_text("<script>untrusted text</script>")
            page.reload()
            expect(page.locator("#messages p")).to_have_count(2)
            page.screenshot(path=str(tmp_path / "conversations.png"), full_page=True)
            page.get_by_role("link", name="Account", exact=True).click()
            expect(page.locator("#signed-in")).to_be_visible()
            assert not errors
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.locator("#signed-out")).to_be_visible()
            page.screenshot(path=str(tmp_path / "identity-login.png"), full_page=True)
        finally:
            browser.close()
