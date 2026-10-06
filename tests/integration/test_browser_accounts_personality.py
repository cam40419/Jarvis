import os

import pytest

from simon.adapters.postgres import PostgresStore
from tests.contract.test_connected import connected_setup
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_invitation_signup_private_home_and_personality_ui(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    _, _, token = connected_setup(PostgresStore(postgres_url))
    with (
        running_api(
            postgres_url,
            tmp_path / "accounts-browser.log",
            public_path="/simon",
            hostname="localhost",
        ) as api,
        sync_playwright() as pw,
    ):
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            origin = str(api.base_url).rstrip("/")
            context = browser.new_context(viewport={"width": 1360, "height": 950})
            context.add_cookies(
                [{"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"}]
            )
            admin = context.new_page()
            errors = []
            admin.on("pageerror", lambda error: errors.append(str(error)))
            admin.goto(origin + "/simon/login")
            expect(admin.locator("#account-management")).to_be_visible()
            admin.locator("#invite-name").fill("Alex")
            admin.locator("#invite-submit").click()
            expect(admin.locator("#invitation-result")).to_be_visible()
            invitation = admin.locator("#invitation-token").input_value()
            expect(admin.locator(".managed-account")).to_contain_text("Alex")
            admin.goto(origin + "/simon/chat")
            expect(admin.locator("#connections-open")).to_be_enabled()
            admin.locator("#personality-open").click()
            admin.locator("#persona-preset").select_option("jarvis")
            expect(admin.locator("#persona-voice")).to_have_value("vesper")
            expect(admin.locator("#persona-address")).to_have_value("sir")
            admin.locator("#persona-instructions").fill("Keep technical answers concise.")
            admin.locator("#persona-save").click()
            expect(admin.locator("#personality-status")).to_contain_text("Saved for you")
            admin.reload()
            expect(admin.locator("#connections-open")).to_be_enabled()
            admin.locator("#personality-open").click()
            expect(admin.locator("#persona-preset")).to_have_value("jarvis")
            expect(admin.locator("#persona-instructions")).to_have_value(
                "Keep technical answers concise."
            )
            admin.set_viewport_size({"width": 390, "height": 844})
            assert admin.locator("#personality-panel").evaluate(
                "e => e.scrollWidth <= e.clientWidth + 1"
            )
            admin.screenshot(path=str(tmp_path / "personality-mobile.png"))

            other = browser.new_context()
            member = other.new_page()
            member.on("pageerror", lambda error: errors.append(str(error)))
            cdp = other.new_cdp_session(member)
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
            member.goto(origin + "/simon/login")
            member.get_by_text("Set up a passkey", exact=True).click()
            member.locator("#enrollment").fill(invitation)
            member.get_by_role("button", name="Create a passkey", exact=True).click()
            expect(member.locator("#signed-in")).to_be_visible()
            expect(member.locator("#account-management")).to_be_hidden()
            expect(member.locator("#workspaces")).to_contain_text("Alex's workspace")
            member.goto(origin + "/simon/chat")
            expect(member.locator("#connections-open")).to_be_enabled()
            member.locator("#personality-open").click()
            expect(member.locator("#persona-preset")).to_have_value("simon")
            member.get_by_role("button", name="Close personality").click()
            expect(member.locator("#workspace")).to_contain_text("Alex's workspace")
            member.locator("#work-open").click()
            expect(member.locator("#work-view")).to_be_visible()
            expect(member.locator("#home-open")).to_have_count(0)
            admin.goto(origin + "/simon/login")
            expect(admin.locator(".managed-account")).to_contain_text("active")
            admin.get_by_role("button", name="Disable account", exact=True).click()
            expect(admin.locator("#accounts-status")).to_contain_text("sessions revoked")
            member.reload()
            expect(member).to_have_url(origin + "/simon/login")
            expect(member.locator("#signed-out")).to_be_visible()
            assert errors == []
        finally:
            browser.close()
