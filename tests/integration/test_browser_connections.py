import os
from pathlib import Path
from uuid import uuid4

import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.connected_tools import WebSource
from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.home import HomeCommand, HomeOrganization, HomeStatus
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_connected import connected_setup, make_proposal
from tests.contract.test_home_tools import prepare
from tests.contract.test_model_runs import FakeModel
from tests.integration.test_restart import running_api
from tests.unit.test_home_adapters import device

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_browser_connection_cards_citations_reload_and_mobile(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 for the browser UI check")
    from playwright.sync_api import expect, sync_playwright

    service, actor, token = connected_setup(PostgresStore(postgres_url))
    action, thread, _ = make_proposal(service, actor)
    calendar, calendar_thread, _ = make_proposal(service, actor, "propose_calendar_event")
    model = FakeModel()
    original = model.generate
    model.generate = lambda request: original(request).model_copy(
        update={
            "web_sources": (
                WebSource.model_validate(
                    {"title": "A test source", "url": "https://example.com/test"}
                ),
            ),
            "tool_calls": ("web_search_call",),
        }
    )
    conversations = ModelConversationService(service.store, service.audit, model, service.settings)
    web_thread = conversations.create(
        actor, CreateThread(title="Web sources", idempotency_key="browser-web-source")
    )
    conversations.submit(
        actor, web_thread.id, SubmitRun(text="Find a source", idempotency_key="browser-web-answer")
    )
    log_path = tmp_path / "connected-browser.log"
    with running_api(postgres_url, log_path) as api, sync_playwright() as playwright:
        origin = str(api.base_url).rstrip("/")
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
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/chat#" + str(thread.id))
            expect(page.get_by_role("region", name="Email preview")).to_be_visible()
            expect(page.get_by_role("button", name="Confirm & send email")).to_be_visible()
            expect(page.locator(".action-card")).to_contain_text("test@example.com")
            expect(page.locator(".action-card")).to_contain_text("From: owner@example.com")
            page.reload()
            expect(page.locator(".action-card")).to_contain_text("A synthetic test.")
            # Exercise the UI receipt path with a simulated response, never Gmail.
            confirmations = []

            def confirm(route):
                confirmations.append(route.request.method)
                route.fulfill(
                    json=action.model_copy(update={"status": "succeeded"}).model_dump(mode="json")
                )

            page.route("**/v1/actions/*/confirm", confirm)
            page.get_by_role("button", name="Confirm & send email").click()
            expect(page.locator(".action-status")).to_contain_text("Email sent")
            assert confirmations == ["POST"]
            expect(page.get_by_role("button", name="Confirm & send email")).to_have_count(0)
            page.goto(origin + "/chat#" + str(calendar_thread.id))
            expect(page.get_by_role("region", name="Calendar preview")).to_be_visible()
            expect(page.locator(".action-card")).to_contain_text("2026-10-01T12:00:00-04:00")
            page.get_by_role("button", name="Cancel preview").click()
            expect(page.locator(".action-status")).to_contain_text("Preview cancelled")
            page.reload()
            expect(page.locator(".action-status")).to_contain_text("Preview cancelled")
            assert service.get_action(actor, calendar.id).status == "cancelled"
            page.goto(origin + "/chat#" + str(web_thread.id))
            page.locator(".answer-sources summary").click()
            expect(page.get_by_role("link", name="A test source")).to_have_attribute(
                "href", "https://example.com/test"
            )
            page.locator("#connections-open").click()
            expect(page.locator("#connections-panel")).to_be_visible()
            expect(page.locator("#google-connection")).to_contain_text("owner@example.com")
            expect(page.locator("#google-connect")).to_be_disabled()
            page.get_by_role("button", name="Close connections").click()
            page.goto(origin + "/chat#" + str(thread.id))
            expect(page.locator(".action-card")).to_be_visible()
            artifacts = Path(".local/screenshots")
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(artifacts / "simon-action-preview-desktop.png"), full_page=True
            )
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator(".action-card")).to_be_visible()
            page.get_by_role("button", name="Confirm & send email").click(trial=True)
            page.locator("#sidebar").evaluate(
                "sidebar => Promise.all(sidebar.getAnimations().map("
                "animation => animation.finished))"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / "simon-action-preview-mobile.png"), full_page=True)
            assert not errors
        finally:
            browser.close()
        # Verify that even rejected OAuth callback credentials are absent from server logs.
        assert (
            api.get("/auth/google/callback?state=synthetic-state&code=synthetic-code").status_code
            == 303
        )
    assert "synthetic-code" not in log_path.read_text()


def test_browser_home_cards_and_status(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 for the browser UI check")
    from playwright.sync_api import expect, sync_playwright

    service, actor, token = connected_setup(PostgresStore(postgres_url))
    light = device().model_copy(
        update={
            "id": "office-beam",
            "household_id": actor.household_id,
            "room": "Office",
            "load_type": "lighting",
            "control_enabled": True,
        }
    )
    service.home.devices = (light,)
    state = HomeStatus(
        device_id=light.id,
        online=True,
        on=True,
        brightness=50,
        capabilities=("power", "brightness"),
    )
    service.home.lifx.read = lambda device: state
    action = prepare(service, actor)
    thread_id = service.store.run(action.run_id).thread_id
    command = HomeCommand(
        id=uuid4(),
        household_id=actor.household_id,
        actor_id=actor.actor_id,
        thread_id=thread_id,
        run_id=action.run_id,
        device_name="Office Beam",
        change=action.home,
        status="succeeded",
        observed=state,
        verified=True,
    )
    service.store.save_home_command(command)
    with (
        running_api(postgres_url, tmp_path / "home-browser.log") as api,
        sync_playwright() as playwright,
    ):
        origin = str(api.base_url).rstrip("/")
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
            errors, calls = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(
                "**/v1/home/devices",
                lambda route: route.fulfill(json=service.home.inventory(actor)),
            )

            def status(route):
                calls.append("read")
                route.fulfill(json=state.model_dump(mode="json"))

            page.route("**/v1/home/devices/office-beam/status", status)
            page.goto(origin + "/chat#" + str(thread_id))
            expect(page.get_by_role("region", name="Device result")).to_be_visible()
            expect(page.locator(".action-card")).to_contain_text("Office Beam")
            expect(page.locator(".action-card")).to_contain_text("50% brightness")
            expect(page.locator(".action-card")).to_contain_text("Reported device state matches")
            assert not calls
            page.locator("#connections-open").click()
            expect(page.locator("#home-devices")).to_contain_text("Office Beam")
            page.locator("#home-devices").get_by_role("button", name="Check status").click()
            expect(page.locator("#home-devices")).to_contain_text("50% brightness")
            assert calls == ["read"]
            edits = []

            def organize(route):
                edits.append(route.request.post_data_json)
                result = service.home.catalog.organize(
                    actor,
                    HomeOrganization.model_validate(edits[-1]),
                    service.home.devices,
                    operation_key="browser-room-edit",
                )
                route.fulfill(json=result)

            page.route("**/v1/home/organize", organize)
            page.get_by_text("Edit room & groups", exact=True).click()
            page.get_by_label("Room", exact=True).fill("Study")
            page.get_by_label("Groups (one per line)").fill("Desk lights\nEvening")
            page.get_by_role("button", name="Save room & groups").click()
            expect(page.locator("#home-devices")).to_contain_text("Study")
            expect(page.locator("#home-devices")).to_contain_text("Groups: Desk lights, Evening")
            assert edits == [
                {"device_ids": [light.id], "room": "Study", "groups": ["Desk lights", "Evening"]}
            ]
            page.get_by_role("button", name="Refresh devices", exact=True).click()
            expect(page.locator("#home-devices")).to_contain_text("Groups: Desk lights, Evening")
            page.get_by_role("button", name="Close connections").click()
            artifacts = Path(".local/screenshots")
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "simon-home-control-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.get_by_role("button", name="Confirm & apply change")).to_have_count(0)
            page.locator("#sidebar").evaluate(
                "sidebar => Promise.all(sidebar.getAnimations().map("
                "animation => animation.finished))"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(artifacts / "simon-home-control-mobile.png"), full_page=True)

            page.reload()
            expect(page.get_by_role("region", name="Device result")).to_contain_text(
                "Reported device state matches"
            )
            expect(page.get_by_role("button", name="Confirm & apply change")).to_have_count(0)
            assert calls == ["read"]
            assert not errors
        finally:
            browser.close()
