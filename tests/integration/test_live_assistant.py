import os

import pytest

from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser, pytest.mark.live]


def test_live_browser_assistant_uses_saved_memory(postgres_url, tmp_path):
    if os.environ.get("JARVIS_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set JARVIS_LIVE_MODEL_TESTS=1 for the paid synthetic live check")
    from playwright.sync_api import expect, sync_playwright

    from jarvis.seed import seed_development_identity

    seed_development_identity(postgres_url)
    with (
        running_api(
            postgres_url, tmp_path / "live-api.log", hostname="localhost", model_provider="openai"
        ) as api,
        sync_playwright() as playwright,
    ):
        options = {"headless": True}
        if os.environ.get("JARVIS_BROWSER_CHANNEL"):
            options["channel"] = os.environ["JARVIS_BROWSER_CHANNEL"]
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page()
            page.goto(str(api.base_url).rstrip("/") + "/login")
            page.get_by_text("Local development login", exact=True).click()
            page.locator("#dev-token").fill("process-development-secret-32-characters")
            page.get_by_role("button", name="Open development workspace").click()
            page.get_by_role("link", name="Open conversations").click()
            expect(page.locator("#assistant-mode")).to_contain_text("Ask Jarvis")
            page.locator("#title").fill("Live assistant verification")
            page.get_by_role("button", name="Create conversation", exact=True).click()
            expect(page.locator("#status")).to_have_text("Conversation created.")
            page.get_by_text("Shared household memories", exact=True).click()
            page.locator("#memory-subject").fill("Planning codename")
            page.locator("#memory-content").fill("Our household planning codename is Maple.")
            page.get_by_role("button", name="Save shared memory").click()
            expect(page.locator("#status")).to_have_text("Shared memory saved.")
            page.locator("#text").fill(
                "What is our saved household planning codename? Reply with just the codename."
            )
            with page.expect_response(
                lambda response: "/runs" in response.url and response.request.method == "POST",
                timeout=65000,
            ) as result:
                page.get_by_role("button", name="Send", exact=True).click()
            assert result.value.status == 201
            run = result.value.json()
            assert run["model_provider"] == "openai" and run["provider_response_id"]
            assert run["input_tokens"] > 0 and run["output_tokens"] > 0
            assert len(run["memory_context"]) == 1
            expect(page.locator("#status")).to_have_text("Run complete. Messages saved.")
            expect(page.locator("#messages")).to_contain_text("Jarvis: Maple")
            page.reload()
            expect(page.locator("#messages")).to_contain_text("Jarvis: Maple")
            page.screenshot(path=str(tmp_path / "live-assistant.png"), full_page=True)
        finally:
            browser.close()
