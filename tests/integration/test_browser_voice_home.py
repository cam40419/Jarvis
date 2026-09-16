import os
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.models import utc_now
from tests.contract.test_connected import connected_setup
from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser]

FAKE_RTC = """
window.__streams = [];
window.__media = async () => {
  const context = new AudioContext();
  const stream = context.createMediaStreamDestination().stream;
  window.__streams.push(stream); return stream;
};
navigator.mediaDevices.getUserMedia = window.__media;
window.RTCPeerConnection = class extends EventTarget {
  constructor() { super(); window.__peer = this; this.iceGatheringState = 'complete'; }
  addTrack() {}
  createDataChannel() { this.channel = {readyState: 'open', send() {}}; return this.channel; }
  async createOffer() { return {type: 'offer', sdp: 'v=0\\r\\nsynthetic'}; }
  async setLocalDescription(value) { this.localDescription = value; }
  async setRemoteDescription() {
    this.connectionState = 'connected'; this.onconnectionstatechange();
  }
  close() { this.connectionState = 'closed'; this.onconnectionstatechange?.(); }
};
window.__caption = (speaker, delta, id) => window.__peer.channel.onmessage({data: JSON.stringify({
  type: `session.${speaker}_transcript.delta`, delta, event_id: id, start_ms: 0, end_ms: 100,
})});
"""


def test_voice_home_and_chat_under_public_prefix(postgres_url, tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    _, _, token = connected_setup(PostgresStore(postgres_url))
    with (
        running_api(postgres_url, tmp_path / "voice-browser.log", public_path="/simon") as api,
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
            context.add_init_script(FAKE_RTC)
            page = context.new_page()
            errors, calls = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            session_id, thread_id = str(uuid4()), str(uuid4())
            page.route(
                "**/simon/v1/voice",
                lambda route: route.fulfill(
                    json={
                        "enabled": True,
                        "max_seconds": 900,
                        "sessions": [{"id": session_id, "created_at": utc_now().isoformat()}]
                        if calls
                        else [],
                    }
                ),
            )

            def voice(route):
                path = route.request.url.split("/simon")[-1]
                calls.append(path)
                if route.request.method == "POST":
                    assert route.request.headers["x-csrf-token"]
                if path == "/v1/voice/sessions":
                    route.fulfill(
                        json={
                            "id": session_id,
                            "thread_id": thread_id,
                            "sdp": "v=0\r\nanswer",
                            "max_seconds": 900,
                        }
                    )
                elif path == "/v1/voice/sessions/" + session_id:
                    route.fulfill(
                        json={
                            "state": "active",
                            "seconds": 4,
                            "backend_status": "Ready",
                            "thread_id": thread_id,
                            "usage_final": True,
                            "fragments": [{"speaker": "user", "text": "A saved voice transcript."}],
                        }
                    )
                else:
                    route.fulfill(json={"status": "ok"})

            page.route("**/simon/v1/voice/sessions**", voice)
            page.goto(origin + "/simon/chat")
            expect(page.locator("#connection")).to_contain_text("Offline test mode")
            page.locator("#voice-open").click()
            page.locator("#voice-start").click()
            expect(page.locator("#voice-status")).to_contain_text("Connected")
            page.evaluate(
                "__caption('input', 'Hello Simon', 'u1'); "
                "__caption('output', 'Hello there.', 'a1'); "
                "__caption('input', 'Wait, stop.', 'u2')"
            )
            expect(page.locator(".voice-caption")).to_have_count(3)
            page.evaluate("__caption('input', 'Wait, stop.', 'u2')")
            expect(page.locator(".voice-caption")).to_have_count(3)
            page.locator("#voice-mute").click()
            assert page.evaluate("__streams[0].getAudioTracks()[0].enabled") is False
            page.locator("#voice-mute").click()
            page.locator("#voice-stop-task").click()
            expect(page.locator("#voice-task-status")).to_contain_text("Task stopped")
            assert (
                page.locator("#voice-results").get_attribute("href") == "/simon/chat#" + thread_id
            )
            page.locator("#voice-end").click()
            expect(page.locator("#voice-status")).to_contain_text("Call ended")
            assert page.evaluate("__streams[0].getAudioTracks()[0].readyState") == "ended"
            assert "/v1/voice/sessions/" + session_id + "/close" in calls
            # End while the browser permission request is still pending.
            page.evaluate(
                "() => { navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { "
                "window.__allow = resolve; }); }"
            )
            page.locator("#voice-start").click()
            page.locator("#voice-end").click()
            page.evaluate("async () => __allow(await __media())")
            page.wait_for_function(
                '() => __streams.at(-1).getAudioTracks()[0].readyState === "ended"'
            )
            page.evaluate(
                "() => { navigator.mediaDevices.getUserMedia = async () => { "
                'throw new DOMException("Denied", "NotAllowedError"); }; }'
            )
            page.locator("#voice-start").click()
            expect(page.locator("#voice-status")).to_contain_text("permission was denied")
            page.locator("#voice-close").click()
            page.locator("#text").fill("Synthetic prefix smoke test")
            page.locator("#send-button").click()
            expect(page.locator(".message.assistant")).to_contain_text(
                "Synthetic prefix smoke test"
            )
            assert "/simon/chat#" in page.url

            device = {
                "id": "office-outlet",
                "name": "Office purifier",
                "room": "Office",
                "provider": "shelly",
                "load_type": "air_purifier",
                "present": True,
                "control_enabled": True,
                "setup_available": True,
            }
            page.route("**/simon/v1/home/devices", lambda route: route.fulfill(json=[device]))
            page.route(
                "**/simon/v1/home/power",
                lambda route: route.fulfill(
                    json={"poll_seconds": 60, "monitoring_enabled": True, "devices": []}
                ),
            )
            page.route(
                "**/simon/v1/home/devices/*/status",
                lambda route: route.fulfill(
                    json={
                        "device_id": device["id"],
                        "online": True,
                        "on": False,
                        "watts": 0,
                        "capabilities": ["power"],
                    }
                ),
            )
            writes = []

            def control(route):
                writes.append(route.request.post_data_json)
                route.fulfill(json={"status": "succeeded", "verified": True})

            page.route("**/simon/v1/home/devices/*/control", control)
            samples = [
                {
                    "captured_at": (utc_now() - timedelta(minutes=n)).isoformat(),
                    "state": "ok",
                    "reading": {"watts": 10 + n},
                }
                for n in (20, 19, 2, 1)
            ]
            page.route(
                "**/simon/v1/home/devices/*/power?*",
                lambda route: route.fulfill(
                    json={
                        "samples": samples,
                        "summary": {
                            "observed_energy_kwh": 0.012,
                            "covered_seconds": 120,
                            "requested_seconds": 86400,
                            "gaps": 1,
                            "counter_resets": 0,
                        },
                    }
                ),
            )
            page.locator("#home-open").click()
            card = page.locator(".home-card")
            expect(card).to_contain_text("Office purifier")
            card.get_by_role("button", name="Turn on", exact=True).click()
            expect(card).to_contain_text("Updated and verified")
            assert writes[0]["on"] is True and writes[0]["idempotency_key"]
            card.locator("summary").click()
            expect(card).to_contain_text("0.012 kWh observed")
            expect(card.locator("polyline")).to_have_count(2)
            expect(card).to_contain_text("0 meter resets")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("#home-panel").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            page.screenshot(path=str(tmp_path / "home-mobile.png"))
            page.locator("#home-close").click()
            page.locator("#voice-open").click()
            page.locator("#voice-history").select_option(session_id)
            expect(page.locator("#voice-captions")).to_contain_text("A saved voice transcript.")
            assert page.locator("#voice-panel").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            page.screenshot(path=str(tmp_path / "voice-mobile.png"))
            assert errors == []
        finally:
            browser.close()
