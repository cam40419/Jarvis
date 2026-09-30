import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.browser


def test_sequence_repeats_are_editable_and_saved_batches_can_be_repeated():
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1 for the browser UI check")
    from playwright.sync_api import expect, sync_playwright

    static = Path(__file__).resolve().parents[2] / "src" / "simon" / "api" / "static"
    jobs = [
        {"id": str(index), "position": index, "label": name, "state": "done",
         "progress": 100, "swap_enabled": int(index == 0), "sha256": "a" * 64,
         "use_ams": int(index == 0), "ams_slot": 3 if index == 0 else None}
        for index, name in enumerate(("A.3mf", "B.3mf"))
    ]
    batches = [{"id": "completed", "state": "done", "created": 1789680669, "jobs": jobs}]
    repeats, starts, unexpected, errors = [], [], [], []

    def route_request(route):
        path = urlparse(route.request.url).path
        if path == "/automations":
            route.fulfill(content_type="text/html", body=(static / "automations.html").read_bytes())
        elif path.startswith("/assets/"):
            asset = static / path.removeprefix("/assets/")
            route.fulfill(content_type=mimetypes.guess_type(asset.name)[0] or "text/plain", body=asset.read_bytes())
        elif path == "/auth/session":
            route.fulfill(json={"scopes": ["home:read", "jobs:write"], "csrf_token": "test"})
        elif path == "/v1/printers/a1/status":
            route.fulfill(json={"configured": True, "online": True, "state": "FINISH",
                                "homed_axes": "XYZ", "alerts": 0, "progress_percent": 100})
        elif path == "/v1/printers/a1/swap-sequence":
            route.fulfill(json={"gcode": "G90\nG1 X10 F1000\nG90\n", "active_trial_id": "verified"})
        elif path == "/v1/printers/a1/batches":
            route.fulfill(json=batches)
        elif path == "/v1/printers/a1/batches/completed/repeat":
            assert route.request.method == "POST"
            assert route.request.headers["x-csrf-token"] == "test"
            body = route.request.post_data_json
            repeats.append(body)
            repeated_jobs = [
                {**job, "id": f"new-{index}", "position": index, "state": "queued",
                 "progress": None, "swap_enabled": int(index < len(jobs) * body["count"] - 1)}
                for index, job in enumerate(jobs * body["count"])
            ]
            batches.insert(0, {"id": "newbatch", "state": "staged", "created": 1789685000, "jobs": repeated_jobs})
            route.fulfill(status=201, json={"id": "newbatch", "jobs": repeated_jobs})
        elif path == "/v1/printers/a1/batches/newbatch/start":
            starts.append(route.request.post_data_json)
            batches[0]["state"] = "running"
            batches[0]["auto_continue"] = starts[-1]["auto_continue"]
            route.fulfill(json={"id": "newbatch", "state": "running"})
        elif path == "/v1/workflows/health":
            route.fulfill(json={"worker_online": True})
        elif path in {"/v1/workflows", "/v1/workflow-runs", "/v1/workflow-schedules",
                      "/v1/workflow-triggers", "/v1/home/devices",
                      "/v1/printers/a1/swap-trials"}:
            route.fulfill(json=[])
        else:
            unexpected.append(path)
            route.fulfill(status=404, json={"error": {"message": "Unexpected test request"}})

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
            expect(page.locator("#batch-completed")).to_be_visible()
            page.locator("#batch-files").set_input_files([
                {"name": name, "mimeType": "application/octet-stream", "buffer": b"sliced file"}
                for name in ("A.3mf", "B.3mf")
            ])
            page.locator(".print-ams input").nth(0).check()
            page.get_by_label("AMS Lite slot for print 1", exact=True).select_option("3")
            page.get_by_label("Backup AMS Lite slot for print 1", exact=True).select_option("4")
            page.locator("#sequence-repeat").fill("2")
            page.locator("#repeat-sequence").click()
            expect(page.locator(".print-step")).to_have_count(4)
            expect(page.locator(".swap-step")).to_have_count(3)
            for index, file_index in enumerate(("0", "1", "0", "1"), 1):
                expect(page.get_by_label(f"File for print {index}", exact=True)).to_have_value(file_index)
            expect(page.locator(".print-ams input").nth(2)).to_be_checked()
            expect(page.get_by_label("AMS Lite slot for print 3", exact=True)).to_have_value("3")
            expect(page.get_by_label("Backup AMS Lite slot for print 3", exact=True)).to_have_value("4")
            page.get_by_label("AMS Lite slot for print 3", exact=True).select_option("4")
            expect(page.get_by_label("AMS Lite slot for print 1", exact=True)).to_have_value("3")
            page.locator("#repeat-count-completed").fill("2")
            page.locator("#refresh").click()
            expect(page.locator("#repeat-count-completed")).to_have_value("2")
            page.locator("#batch-completed").get_by_role("button", name="Repeat sequence", exact=True).click()
            expect(page.locator("#batch-newbatch .batch-job")).to_have_count(4)
            expect(page.locator("#batch-newbatch .batch-swap-event").first).to_contain_text("Swap inside print file")
            assert not starts
            mode = page.get_by_label("Between prints for batch newbatch", exact=True)
            expect(mode).to_have_value("automatic")
            mode.select_option("manual")
            page.locator("#refresh").click()
            expect(mode).to_have_value("manual")
            mode.select_option("automatic")
            page.locator("#batch-newbatch").get_by_role("checkbox").check()
            page.locator("#refresh").click()
            expect(page.locator("#batch-newbatch").get_by_role("checkbox")).to_be_checked()
            page.locator("#batch-newbatch").get_by_role("button", name="Start batch", exact=True).click()
            expect(page.locator("#batch-newbatch h3")).to_contain_text("running")
            expect(page.locator("#batch-newbatch .batch-swap-event").first).to_contain_text("next print automatically")
            assert starts == [{"operator_present": True, "plate_checked": False, "auto_continue": True}]
            batches[0]["state"] = "waiting_for_plate"
            batches[0]["auto_continue"] = False
            batches[0]["jobs"][0].update(state="done", progress=100)
            page.locator("#refresh").click()
            expect(page.locator("#batch-newbatch .batch-job").first).to_contain_text("done · 100%")
            expect(page.locator("#batch-newbatch .batch-swap-event").first).to_contain_text("check new plate")
            mode.select_option("manual")
            page.locator("#batch-newbatch").get_by_role("checkbox").check()
            page.locator("#refresh").click()
            expect(page.locator("#batch-newbatch").get_by_role("checkbox")).to_be_checked()
            page.locator("#batch-newbatch").get_by_role("button", name="Continue to next print").click()
            expect(page.locator("#batch-newbatch h3")).to_contain_text("running")
            assert starts[-1] == {"operator_present": True, "plate_checked": True, "auto_continue": False}
            batches[0]["state"] = "needs_attention"
            batches[0]["jobs"][1].update(state="uncertain")
            page.locator("#refresh").click()
            expect(page.locator("#batch-newbatch").get_by_role("button", name="Print and swap finished", exact=True)).to_have_count(1)
            expect(page.locator("#batch-newbatch").get_by_role("button", name="Retry pending swap only", exact=True)).to_have_count(0)
            assert len(starts) == 2
            assert repeats == [{"count": 2}]
            assert not unexpected
            assert not errors
        finally:
            browser.close()
