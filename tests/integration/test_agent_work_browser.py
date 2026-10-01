"""Exercise the Work UI against real API services without network model calls."""

import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from simon.adapters.tool_preflight import INSTALLED_TRANSPORTS
from simon.adapters.tool_transports import TransportRegistry
from simon.agent_setup import starter_manifest
from simon.api.app import AppContainer, create_app
from simon.config import Settings
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_worker import AgentWorker

pytestmark = pytest.mark.browser


@pytest.fixture
def agent_ui(tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    container = AppContainer(
        settings=Settings(
            _env_file=None,
            model_provider="local",
            openai_api_key=None,
            agent_manifest_file=None,
            environment="test",
            storage_backend="memory",
            public_origin=origin,
            rp_id="127.0.0.1",
            dev_login_enabled=True,
            dev_login_token=SecretStr("test-development-secret-32-characters"),
            agent_state_dir=tmp_path / "agents",
        )
    )
    manifest = PlatformManifest(
        agents=(AgentProfile(id="writer", name="Report writer", instructions="Write clearly."),),
        teams=(TeamTemplate(id="studio", name="Research studio", agent_ids=("writer",)),),
        models=(
            ModelEndpoint(
                id="local",
                model="synthetic-local",
                provider="openai_compatible",
                base_url="http://localhost:11434/v1",
                local=True,
                tier="economy",
            ),
        ),
    )
    configured = AgentPlatformService(container.store, manifest, state_dir=tmp_path, environ={})
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_runs.enabled = True

    class Model:
        def generate(self, decision, request):
            return TextGenerationResult(
                endpoint_id=decision.endpoint_id,
                model=decision.model,
                text="## Completed report\nA useful, saved result.",
                input_tokens=20,
                output_tokens=8,
            )

    dispatcher = AgentDispatcher(
        container.agent_runs,
        worker_factory=lambda *_: AgentWorker(
            Model(),
            container.agent_platform.tools,
            TransportRegistry(),
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(container),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    with httpx.Client(base_url=origin, trust_env=False) as client:
        response = client.post(
            "/auth/dev-login",
            headers={"Origin": origin},
            json={"token": "test-development-secret-32-characters"},
        )
        assert response.status_code == 200
        token = client.cookies.get("simon_session")
    try:
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.environ.get("SIMON_BROWSER_CHANNEL"):
                options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1440, "height": 1080})
            context.add_cookies(
                [
                    {"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"},
                ]
            )
            errors = []
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/chat")
            expect(page.locator("#connections-open")).to_be_enabled()
            page.get_by_role("button", name="Work", exact=True).click()
            expect(page.locator("#agent-create")).to_be_enabled()
            try:
                yield page, dispatcher, container
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_create_plan_run_download_and_return_to_saved_result(agent_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, _ = agent_ui
    page.get_by_role("button", name="New agent plan", exact=True).click()
    page.get_by_label("What should this task produce?").fill("Write a useful report.")
    page.get_by_role("button", name="Review plan", exact=True).click()
    expect(page.locator("#agent-detail")).to_contain_text("synthetic-local")
    expect(page.locator("#agent-detail")).to_contain_text("Ready to start")
    page.get_by_label("Model budget (USD, optional)").fill("0")
    page.get_by_role("button", name="Start run", exact=True).click()
    expect(page.locator("#agent-detail")).to_contain_text("Waiting for dispatcher")
    completed = dispatcher.tick()
    assert completed.status == "succeeded"
    page.locator("#agent-refresh").click()
    expect(page.locator("#agent-detail")).to_contain_text("A useful, saved result.")
    with page.expect_download() as downloaded:
        page.get_by_role("link", name="answer.txt").click()
    download = downloaded.value
    assert download.suggested_filename == "answer.txt"
    assert "A useful, saved result." in Path(download.path()).read_text(encoding="utf-8")
    page.reload()
    page.get_by_role("button", name="Work", exact=True).click()
    expect(page.locator("#agent-detail")).to_contain_text("A useful, saved result.")
    page.screenshot(path=str(tmp_path / "agent-work-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#agent-detail")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "agent-work-mobile.png"), animations="disabled")


def test_dependency_loop_is_rejected_and_queued_run_can_be_cancelled(agent_ui):
    from playwright.sync_api import expect

    page, _, _ = agent_ui
    page.get_by_role("button", name="New agent plan", exact=True).click()
    page.locator("#task-1-objective").fill("Gather source material.")
    page.get_by_role("button", name="Add another task").click()
    page.locator("#task-2-objective").fill("Review the gathered material.")
    page.locator('[data-task-id="task-2"]').get_by_label("task-1", exact=True).check()
    page.locator('[data-task-id="task-1"]').get_by_label("task-2", exact=True).check()
    page.get_by_role("button", name="Review plan", exact=True).click()
    expect(page.locator("#agent-plan-status")).to_contain_text("create a loop")
    page.locator('[data-task-id="task-1"]').get_by_label("task-2", exact=True).uncheck()
    page.get_by_role("button", name="Review plan", exact=True).click()
    expect(page.locator("#agent-detail")).to_contain_text("Stage 2")
    page.get_by_role("button", name="Start run", exact=True).click()
    page.get_by_role("button", name="Cancel run", exact=True).click()
    expect(page.locator("#agent-detail .agent-detail-heading")).to_contain_text("Cancelled")
    page.get_by_role("tab", name="Tools & setup").click()
    expect(page.locator("#agent-browser")).to_contain_text("Available tool templates")
    page.get_by_role("tab", name="Tools & setup").press("ArrowLeft")
    expect(page.get_by_role("tab", name="Plans")).to_have_attribute("aria-selected", "true")


def test_blocked_plan_explains_setup_and_does_not_offer_start(agent_ui):
    from playwright.sync_api import expect

    page, _, container = agent_ui
    manifest = container.agent_platform.manifest.model_copy(update={"models": ()})
    replacement = AgentPlatformService(
        container.store,
        manifest,
        state_dir=container.agent_platform.state_dir,
        environ={},
    )
    container.agent_platform.__dict__.update(replacement.__dict__)
    page.get_by_role("button", name="New agent plan", exact=True).click()
    page.get_by_label("What should this task produce?").fill("Draft a report.")
    page.get_by_role("button", name="Review plan", exact=True).click()
    expect(page.locator("#agent-detail")).to_contain_text("Needs setup")
    expect(page.locator(".agent-blocked-reason")).not_to_be_empty()
    expect(page.get_by_role("button", name="Start run", exact=True)).to_have_count(0)


def test_rich_starter_catalog_search_keyboard_setup_and_mobile(agent_ui, tmp_path):
    from playwright.sync_api import expect

    page, _, container = agent_ui
    manifest = starter_manifest(
        Settings(_env_file=None, model_provider="local", openai_api_key=None)
    )
    manifest = manifest.model_copy(
        update={
            "models": tuple(
                model.model_copy(
                    update={
                        "enabled": True,
                        "base_url": "https://private-provider-config.invalid/v1",
                        "api_key_env": "PRIVATE_MODEL_ENV_SENTINEL",
                    }
                )
                for model in manifest.models
            ),
            "tools": tuple(
                tool.model_copy(
                    update={
                        "credential_env": "PRIVATE_DROPBOX_ENV_SENTINEL",
                        "settings": tool.settings | {"root_path": "/PRIVATE_ROOT_SENTINEL"},
                    }
                )
                if tool.id == "dropbox.list"
                else tool
                for tool in manifest.tools
            ),
        }
    )
    replacement = AgentPlatformService(
        container.store,
        manifest,
        state_dir=container.agent_platform.state_dir,
        available_transports=INSTALLED_TRANSPORTS,
        environ={"PRIVATE_DROPBOX_ENV_SENTINEL": "synthetic-provider-secret-sentinel"},
    )
    container.agent_platform.__dict__.update(replacement.__dict__)
    with page.expect_response("**/v1/agent-platform/catalog") as response:
        page.get_by_role("button", name="Refresh", exact=True).click()
    catalog = response.value.json()
    assert len(catalog["teams"]) == len(manifest.teams) and len(catalog["tool_statuses"]) > 100
    for private in (
        "private-provider-config.invalid",
        "PRIVATE_MODEL_ENV_SENTINEL",
        "PRIVATE_DROPBOX_ENV_SENTINEL",
        "PRIVATE_ROOT_SENTINEL",
        "synthetic-provider-secret-sentinel",
    ):
        assert private not in response.value.text()
    expect(page.locator("#agent-team-count")).to_have_text(str(len(manifest.teams)))
    page.get_by_role("tab", name="Runs").press("End")
    expect(page.get_by_role("tab", name="Tools & setup")).to_be_focused()
    expect(page.locator("#agent-tool-results .agent-catalog-item")).to_have_count(12)
    expect(page.locator("#agent-tool-count")).to_contain_text(f"of {len(manifest.tools)} tools")
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#agent-tool-count")).to_contain_text("Page 2 of")
    page.get_by_role("button", name="Previous", exact=True).click()
    search = page.get_by_role("searchbox", name="Find a tool")
    search.fill("dropbox")
    expect(page.locator("#agent-tool-results .agent-catalog-item")).to_have_count(5)
    expect(page.locator('[data-tool-id="dropbox.upload"]')).to_contain_text("Disabled")
    expect(page.locator('[data-tool-id="dropbox.upload"]')).to_contain_text("Can make changes")
    expect(page.locator('[data-tool-id="dropbox.upload"]')).to_contain_text(
        "Disabled by the server"
    )
    page.get_by_label("Setup status").select_option("configured")
    expect(page.locator("#agent-tool-results")).to_contain_text("No matching tools")
    page.get_by_label("Setup status").select_option("needs_setup")
    expect(page.locator("#agent-tool-results .agent-catalog-item")).to_have_count(5)
    search.focus()
    with page.expect_response("**/v1/agent-platform/catalog"):
        page.evaluate("document.getElementById('agent-refresh').click()")
    expect(search).to_be_focused()
    expect(search).to_have_value("dropbox")
    page.get_by_label("Setup status").select_option("all")
    search.fill("")
    page.locator(".agent-catalog-heading").evaluate(
        "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
    )
    page.screenshot(path=str(tmp_path / "agent-catalog-desktop.png"), animations="disabled")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    setup = page.locator(".agent-setup-details").first
    setup.locator("summary").focus()
    page.keyboard.press("Enter")
    expect(setup).to_have_attribute("open", "")
    expect(setup).to_contain_text("Server credential is missing")
    page.get_by_role("button", name="New agent plan", exact=True).click()
    expect(page.locator("#agent-team option")).to_have_count(len(manifest.teams))
    page.locator("#agent-plan-dialog").get_by_label("Team", exact=True).select_option(
        "integrations"
    )
    page.get_by_label("Agent profile").select_option("dropbox-reader")
    expect(page.locator(".agent-profile-description")).to_contain_text(
        "Setup needed: 4 tools unavailable"
    )
    page.get_by_label("What should this task produce?").fill(
        "Summarize files in the permitted folder."
    )
    page.get_by_role("button", name="Review plan", exact=True).click()
    expect(page.locator("#agent-detail")).to_contain_text("Needs setup")
    expect(page.locator("#agent-detail")).to_contain_text("dropbox")
    expect(page.get_by_role("button", name="Start run", exact=True)).to_have_count(0)
    page.get_by_role("tab", name="Tools & setup").click()
    search.fill("dropbox")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".agent-tool-filters").evaluate(
        "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
    )
    expect(search).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "agent-catalog-mobile.png"), animations="disabled")
    page.get_by_role("button", name="New agent plan", exact=True).click()
    page.locator("#agent-plan-dialog").get_by_label("Team", exact=True).select_option(
        "integrations"
    )
    expect(page.get_by_label("Agent profile")).to_be_visible()
    assert page.locator("#agent-plan-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.keyboard.press("Escape")
    expect(page.locator("#agent-plan-dialog")).not_to_be_visible()
    expect(page.get_by_role("button", name="New agent plan", exact=True)).to_be_focused()
