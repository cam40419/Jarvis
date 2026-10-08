"""Conversation and connection UI runs independently of project orchestration."""

import os
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from simon.api.app import AppContainer, create_app
from simon.config import Settings
from simon.domain.external_home import HomeChange, HomeCommand

pytestmark = pytest.mark.browser


@pytest.fixture
def chat_ui(tmp_path):
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
            environment="test",
            storage_backend="memory",
            model_provider="local",
            openai_api_key=None,
            public_origin=origin,
            rp_id="127.0.0.1",
            dev_login_enabled=True,
            dev_login_token=SecretStr("test-development-secret-32-characters"),
            local_files_dir=tmp_path / "files",
            local_files_enabled=False,
            integration_key_file=tmp_path / "credentials.key",
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(container), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Isolated test API did not start"
        with httpx.Client(base_url=origin, trust_env=False) as client:
            response = client.post(
                "/auth/dev-login",
                headers={"Origin": origin},
                json={"token": "test-development-secret-32-characters"},
            )
            assert response.status_code == 200
            token = client.cookies.get("simon_session")
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.environ.get("SIMON_BROWSER_CHANNEL"):
                options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
            browser = playwright.chromium.launch(**options)
            try:
                context = browser.new_context(viewport={"width": 1360, "height": 950})
                context.add_cookies(
                    [{"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"}]
                )
                page = context.new_page()
                errors, requests = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(origin + "/chat")
                expect(page.locator("#connections-open")).to_be_enabled()
                yield SimpleNamespace(page=page, container=container, origin=origin)
                assert not errors, errors
                assert not any(
                    part in url
                    for url in requests
                    for part in ("/v1/projects", "/v1/agent-platform", "/v1/project-boards")
                )
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "Isolated test API did not stop"


def test_chat_sends_persists_and_navigates_without_old_work_assets(chat_ui):
    from playwright.sync_api import expect

    page = chat_ui.page
    page.locator("#text").fill("Launch notes for today's meeting")
    page.locator("#text").press("Enter")
    expect(page.locator(".message.assistant")).to_contain_text("Launch notes")
    page.reload()
    expect(page.locator(".message.user")).to_contain_text("Launch notes")
    expect(page.locator(".message.assistant")).to_contain_text("Launch notes")
    expect(page.locator("#work-open, #sidebar-agents, #work-view")).to_have_count(0)
    page.get_by_role("link", name="Projects", exact=True).click()
    expect(page.locator("#np-list-heading")).to_have_text("Projects")
    page.get_by_role("link", name="Conversations", exact=True).click()
    expect(page.locator("#new-chat")).to_be_enabled()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#menu-toggle").click()
    expect(page.get_by_role("link", name="Projects", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_external_home_receipts_remain_visible_without_local_device_proposals(chat_ui):
    from playwright.sync_api import expect

    page = chat_ui.page
    page.locator("#text").fill("Show the studio light receipt")
    page.locator("#text").press("Enter")
    expect(page.locator(".message.assistant")).to_contain_text("studio light")
    command = HomeCommand(
        id=uuid4(),
        workspace_id=uuid4(),
        actor_id=uuid4(),
        device_name="Studio light",
        change=HomeChange(device_id="studio-light", on=True, brightness=35, color="blue"),
        status="succeeded",
        verified=True,
    ).model_dump(mode="json")

    # Substitute only external service receipts, retaining the real conversation and answer API.
    def external_receipt(route):
        response = route.fetch()
        assert response.status == 200
        answers = response.json()
        assert len(answers) == 1
        answers[0]["home_commands"] = [command]
        route.fulfill(response=response, json=answers)

    page.route("**/v1/threads/*/answers*", external_receipt)
    page.reload()
    receipt = page.get_by_role("region", name="Device result")
    expect(receipt).to_contain_text("Studio light")
    expect(receipt).to_contain_text("35% brightness")
    expect(receipt).to_contain_text("Color: blue")
    expect(receipt).to_contain_text("Reported device state matches the request.")
    expect(receipt.get_by_role("button")).to_have_count(0)
    command.update(status="unknown", verified=False)
    page.reload()
    expect(receipt).to_contain_text("Outcome unknown. Check device status")
    expect(page.get_by_role("region", name="Device preview")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(receipt).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
