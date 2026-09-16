import os
from pathlib import Path

import pytest

from simon.adapters.google import ConnectedError
from simon.adapters.postgres import PostgresStore
from simon.domain.home import HomeControl, HomeStatus, OutletPower
from tests.contract.test_home_control import pending
from tests.contract.test_shelly_backend import shelly as shelly_fixture
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]


def test_outlet_ui_setup_power_errors_and_chat(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 for the browser UI check")
    from playwright.sync_api import expect, sync_playwright

    service, actor, token, device, _ = shelly_fixture.__wrapped__(PostgresStore(postgres_url))
    state = {"on": False, "uncertain": False}
    writes, requests = [], []

    def send(target, change):
        writes.append(change.on)
        if state["uncertain"]:
            raise ConnectedError("Device timed out", unknown=True)
        state["on"] = change.on

    service.home.shelly.set = send
    service.home.shelly.read = lambda target: HomeStatus(
        device_id=target.id, online=True, on=state["on"], watts=5, capabilities=("power",)
    )
    with running_api(postgres_url, tmp_path / "outlet-browser.log") as api, sync_playwright() as pw:
        origin = str(api.base_url).rstrip("/")
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context(viewport={"width": 1360, "height": 950})
            context.add_cookies(
                [{"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"}]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def power(route):
                body = route.request.post_data_json
                assert route.request.headers["x-csrf-token"]
                requests.append(body)
                receipt = service.home.outlet_power(
                    actor, device.id, OutletPower.model_validate(body), lambda: actor
                )
                route.fulfill(json=receipt.model_dump(mode="json"))

            page.route("**/v1/home/outlets/*/power", power)
            page.route(
                "**/v1/home/devices/*/status",
                lambda route: route.fulfill(
                    json=service.home.read(actor, device.id).model_dump(mode="json")
                ),
            )
            page.goto(origin + "/chat")
            page.locator("#connections-open").click()
            card = page.locator('[data-device-id="' + device.id + '"]')
            expect(card).to_contain_text("Outlet ID ending 6789ab")
            expect(card.get_by_role("button", name="Turn on", exact=True)).to_be_disabled()
            form = card.get_by_role("form", name="Outlet settings")
            expect(form.get_by_label("Allow control here and in chat")).to_be_disabled()
            form.get_by_label("Device name").fill("Bedroom lamp")
            form.get_by_label("Outlet room").fill("Bedroom")
            form.get_by_label("Powers", exact=True).select_option("lighting")
            expect(form.get_by_label("Allow control here and in chat")).to_be_checked()
            form.get_by_role("button", name="Save outlet").click()
            expect(page.locator(".home-notice")).to_contain_text("Outlet saved")
            expect(card).to_contain_text("Ready for chat control")
            assert writes == []  # The real setup API must not send a switch command.
            configured = service.home.device(actor, device.id)
            assert (configured.name, configured.room, configured.load_type) == (
                "Bedroom lamp",
                "Bedroom",
                "lighting",
            )
            assert configured.control_enabled

            # Persisted UI settings immediately enable room/all-lights control in chat.
            receipt = service.home.control(
                actor,
                pending(service, actor).run.id,
                HomeControl(all_lights=True, on=False),
                lambda: actor,
            )[0]
            assert receipt.verified and receipt.device_name == "Bedroom lamp"
            page.reload()
            page.locator("#connections-open").click()
            expect(form.get_by_label("Device name")).to_have_value("Bedroom lamp")
            expect(form.get_by_label("Powers", exact=True)).to_have_value("lighting")
            card.get_by_role("button", name="Turn on", exact=True).click()
            expect(card.get_by_role("button", name="Turn on", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            expect(card).to_contain_text("Reported device state matches")
            card.get_by_role("button", name="Turn off", exact=True).click()
            expect(card.get_by_role("button", name="Turn off", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert [r["on"] for r in requests] == [True, False]
            assert requests[0]["idempotency_key"] != requests[1]["idempotency_key"]

            state["uncertain"] = True
            card.get_by_role("button", name="Turn on", exact=True).click()
            expect(card).to_contain_text("Outcome unknown")
            expect(card.get_by_role("button", name="Turn on", exact=True)).to_be_disabled()
            expect(card.get_by_role("button", name="Turn off", exact=True)).to_be_disabled()
            assert len(requests) == 3
            card.get_by_role("button", name="Check status", exact=True).click()
            expect(card.get_by_role("button", name="Turn off", exact=True)).to_be_enabled()
            expect(card.get_by_role("button", name="Turn off", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            state["uncertain"] = False

            artifacts = Path(".local/screenshots")
            artifacts.mkdir(parents=True, exist_ok=True)
            card.scroll_into_view_if_needed()
            page.screenshot(path=str(artifacts / "simon-outlet-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            card.get_by_role("button", name="Turn on", exact=True).click(trial=True)
            assert page.locator("#connections-panel").evaluate(
                "panel => panel.scrollWidth <= panel.clientWidth"
            )
            page.screenshot(path=str(artifacts / "simon-outlet-mobile.png"), full_page=True)

            # Changing back to unknown must disable power in the UI and persisted inventory.
            form.get_by_label("Powers", exact=True).select_option("unclassified")
            expect(form.get_by_label("Allow control here and in chat")).not_to_be_checked()
            form.get_by_role("button", name="Save outlet").click()
            expect(card).to_contain_text("Read-only")
            expect(card.get_by_role("button", name="Turn on", exact=True)).to_be_disabled()
            assert not service.home.device(actor, device.id).control_enabled
            assert not errors
        finally:
            browser.close()
