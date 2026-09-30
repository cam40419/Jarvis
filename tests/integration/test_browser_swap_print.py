import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.browser


def test_swap_can_run_through_print_path_without_uploading_an_object():
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 for the browser UI check")
    from playwright.sync_api import expect, sync_playwright

    static = Path(__file__).resolve().parents[2] / "src" / "simon" / "api" / "static"
    batches, requests, errors, unexpected = [], [], [], []
    printer = {"configured": True, "online": True, "state": "IDLE", "homed_axes": "XY"}

    def route_request(route):
        path = urlparse(route.request.url).path
        if path == "/automations":
            route.fulfill(content_type="text/html", body=(static / "automations.html").read_bytes())
        elif path.startswith("/assets/"):
            asset = static / path.removeprefix("/assets/")
            route.fulfill(
                content_type=mimetypes.guess_type(asset.name)[0] or "text/plain",
                body=asset.read_bytes(),
            )
        elif path == "/auth/session":
            route.fulfill(json={"scopes": ["home:read", "jobs:write"], "csrf_token": "test"})
        elif path == "/v1/printers/a1/status":
            route.fulfill(json=printer)
        elif path == "/v1/printers/a1/swap-sequence":
            route.fulfill(json={"gcode": "G90\nG1 X0 F900\nG90\n", "active_trial_id": "active"})
        elif path == "/v1/printers/a1/batches":
            route.fulfill(json=batches)
        elif path == "/v1/printers/a1/swap-print-tests":
            assert route.request.method == "POST"
            assert route.request.headers["x-csrf-token"] == "test"
            requests.append(("prepare", route.request.post_data_json))
            batches.append(
                {
                    "id": "cold",
                    "state": "staged",
                    "created": 1,
                    "jobs": [
                        {
                            "id": "job",
                            "kind": "swap_test",
                            "label": "Swap as print - slow with pauses",
                            "state": "queued",
                            "progress": None,
                        },
                    ],
                }
            )
            route.fulfill(status=201, json={"id": "cold"})
        elif path == "/v1/printers/a1/batches/cold/start":
            requests.append(("start", route.request.post_data_json))
            batches[0]["state"] = "running"
            batches[0]["jobs"][0]["state"] = "running"
            route.fulfill(json={"id": "cold", "state": "running"})
        elif path == "/v1/printers/a1/batches/cold/control":
            requests.append(("control", route.request.post_data_json))
            batches[0]["state"] = "canceled"
            batches[0]["jobs"][0]["state"] = "canceled"
            route.fulfill(json={"id": "cold", "action": "cancel"})
        elif path == "/v1/workflows/health":
            route.fulfill(json={"worker_online": True})
        elif path in {
            "/v1/workflows",
            "/v1/workflow-runs",
            "/v1/workflow-schedules",
            "/v1/workflow-triggers",
            "/v1/home/devices",
            "/v1/printers/a1/swap-trials",
        }:
            route.fulfill(json=[])
        else:
            unexpected.append(path)
            route.fulfill(status=404, json={"error": {"message": "Unexpected request"}})

    with sync_playwright() as pw:
        options = {"headless": True}
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", route_request)
            page.goto("http://simon.test/automations")
            expect(page.locator("#swap-test-gcode")).not_to_be_empty()
            expect(page.locator("#swap-print-source")).to_have_value("active")
            expect(
                page.get_by_role("button", name="Run swap as print", exact=True)
            ).to_be_disabled()
            printer["homed_axes"] = "XYZ"
            page.locator("#refresh").click()
            run = page.get_by_role("button", name="Run swap as print", exact=True)
            expect(run).to_be_enabled()
            edited = "G90\nG1 X0 Y260 F3000\nG90\n"
            page.locator("#swap-test-gcode").fill(edited)
            page.locator("#swap-print-source").select_option("editor")
            page.locator("#swap-print-profile").select_option("slow")
            run.click()
            expect(page.locator("#swap-print-cold")).to_be_visible()
            expect(page.locator("#swap-print-cold")).to_contain_text("running")
            expect(page.locator("#batch-list")).not_to_contain_text("Swap as print")
            expect(run).to_be_disabled()
            page.get_by_role("button", name="Stop test", exact=True).click()
            expect(page.locator("#swap-print-cold")).to_contain_text("canceled")
            expect(run).to_be_enabled()
            assert requests == [
                ("prepare", {"source": "editor", "gcode": edited, "motion_profile": "slow"}),
                ("start", {"operator_present": True}),
                ("control", {"action": "cancel"}),
            ]
            assert not unexpected
            assert not errors
        finally:
            browser.close()
