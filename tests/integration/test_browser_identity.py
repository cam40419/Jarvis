import os

import pytest

from simon.config import Settings
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.services.identity import IdentityService
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_browser_passkey_enrollment_login_logout_and_echo(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 to run the real browser ceremony")
    from playwright.sync_api import expect, sync_playwright

    from simon.adapters.postgres import PostgresStore
    from simon.seed import seed_development_identity

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
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(
                viewport={"width": 1360, "height": 940},
                permissions=["clipboard-read", "clipboard-write"],
            )
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
            expect(page.locator("#status")).to_have_text("Simon is connected.")
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.locator("#signed-out")).to_be_visible()
            page.get_by_role("button", name="Sign in with a passkey").click()
            expect(page.locator("#signed-in")).to_be_visible(timeout=15000)
            page.reload()
            expect(page.locator("#signed-in")).to_be_visible()
            assert len(store.passkeys(DEV_ACTOR_ID)) == 1
            page.get_by_role("link", name="Open conversations").click()
            expect(page.locator("#welcome")).to_be_visible()
            expect(page.locator("#new-chat")).to_be_enabled()
            assert page.evaluate(
                """() => {
                    const output = SimonMarkdown.render('# Heading\\n\\n**Bold** and `code`\\n\\n'
                      + '```js\\n<script>window.injected=true</script>\\n```\\n\\n'
                      + '- One\\n- Two\\n\\n[bad](javascript:alert) [good](https://example.com)\\n\\n'
                      + '| A | B |\\n| --- | --- |\\n| 1 | 2 |');
                    return output.querySelector('h2').textContent === 'Heading'
                      && output.querySelector('strong').textContent === 'Bold'
                      && output.querySelectorAll('li').length === 2
                      && output.querySelectorAll('table').length === 1
                      && output.querySelectorAll('a').length === 1
                      && !output.querySelector('script') && !window.injected;
                }"""
            )
            page.screenshot(path=str(tmp_path / "simon-welcome.png"), full_page=True)
            page.locator("#memory-open").click()
            page.locator("#memory-subject").fill("Dinner preference")
            page.locator("#memory-content").fill("Vegetarian dinners")
            page.get_by_role("button", name="Save shared memory", exact=True).click()
            expect(page.locator("#memory-status")).to_have_text("Shared memory saved.")
            page.get_by_role("button", name="Close memories", exact=True).click()
            page.locator("#text").fill("<script>untrusted text</script>")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("#status")).to_have_text("Run complete. Messages saved.")
            expect(page.locator("#messages .message")).to_have_count(2)
            page.get_by_role("button", name="Helpful", exact=True).click()
            expect(page.get_by_role("button", name="Helpful", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            page.get_by_role("button", name="Copy answer", exact=True).click()
            assert "<script>untrusted text</script>" in page.evaluate(
                "navigator.clipboard.readText()"
            )
            page.locator("#run-profile").click()
            expect(page.locator("#context-details")).to_contain_text("Vegetarian dinners")
            expect(page.locator("#context-details")).to_contain_text("context-v1")
            page.locator("#memory-open").click()
            page.get_by_role("button", name="Retract", exact=True).click()
            expect(page.locator("#memories .memory-card")).to_have_count(0)
            page.get_by_role("button", name="Close memories", exact=True).click()
            expect(page.locator("#messages")).to_contain_text("<script>untrusted text</script>")
            page.reload()
            expect(page.locator("#messages .message")).to_have_count(2)
            expect(page.get_by_role("button", name="Helpful", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            page.get_by_role("button", name="Too slow", exact=True).click()
            expect(page.get_by_role("button", name="Too slow", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            page.get_by_role("button", name="Too slow", exact=True).click()
            expect(page.get_by_role("button", name="Too slow", exact=True)).to_have_attribute(
                "aria-pressed", "false"
            )
            page.screenshot(path=str(tmp_path / "conversations.png"), full_page=True)
            # Exercise Stop before any provider response, with no paid API request.
            page.route(
                "**/v1/assistant",
                lambda route: route.fulfill(json={"provider": "openai", "auto_deep_enabled": True}),
            )
            page.route("**/runs/stream", lambda route: None)
            page.reload()
            page.locator("#response-settings summary").click()
            expect(page.locator("#profile")).to_be_enabled()
            page.locator("#profile").select_option("quick")
            page.locator("#answer-length").select_option("brief")
            page.locator("#auto-deep").uncheck()
            page.locator("#save-preferences").click()
            expect(page.locator("#preferences-status")).to_contain_text("Saved for you")
            page.reload()
            page.locator("#response-settings summary").click()
            expect(page.locator("#profile")).to_have_value("quick")
            expect(page.locator("#answer-length")).to_have_value("brief")
            expect(page.locator("#auto-deep")).not_to_be_checked()
            expect(page.locator("#messages .message")).to_have_count(2)
            page.screenshot(path=str(tmp_path / "simon-preferences.png"), full_page=True)
            page.locator("#profile").select_option("deep")
            page.locator("#answer-length").select_option("brief")
            page.locator("#text").fill("Keep this draft after stopping")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("#stop")).to_be_visible()
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.locator("#status")).to_contain_text("Stopped. Draft kept.")
            expect(page.locator("#text")).to_have_value("Keep this draft after stopping")
            expect(page.locator("#messages .message")).to_have_count(2)
            expect(page.locator("#stop")).to_be_hidden()
            expect(page.locator("#send-button")).to_be_enabled()
            page.locator("#theme-toggle").click()
            expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            page.screenshot(path=str(tmp_path / "simon-dark.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#sidebar")).to_have_css("transform", "matrix(1, 0, 0, 1, -278, 0)")
            page.locator("#theme-toggle").click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / "simon-mobile.png"), full_page=True)
            page.locator("#menu-toggle").click()
            page.locator("#new-chat").click()
            expect(page.locator("#welcome")).to_be_visible()
            expect(page.locator("#text")).to_have_value("")
            page.locator("#menu-toggle").click()
            page.locator(".thread-button").first.click()
            expect(page.locator("#text")).to_have_value("Keep this draft after stopping")
            expect(page.locator("#messages .message")).to_have_count(2)
            page.locator("#menu-toggle").click()
            page.locator(".account-link").click()
            expect(page.locator("#signed-in")).to_be_visible()
            assert not errors
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.locator("#signed-out")).to_be_visible()
            page.screenshot(path=str(tmp_path / "identity-login.png"), full_page=True)
        finally:
            browser.close()
