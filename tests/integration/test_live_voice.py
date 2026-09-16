"""Opt-in, billed WebRTC check using silence and a disposable household database."""

import os
import wave

import pytest

from tests.integration.test_restart import running_api

pytestmark = [pytest.mark.postgres, pytest.mark.browser, pytest.mark.live]


def test_live_voice_webrtc_connects_and_closes(postgres_url, tmp_path):
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1 to run billed synthetic voice check")
    from playwright.sync_api import expect, sync_playwright

    from simon.seed import seed_development_identity

    seed_development_identity(postgres_url)
    silence = tmp_path / "silence.wav"
    with wave.open(str(silence), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x00\x00" * 48000 * 30)
    with (
        running_api(
            postgres_url, tmp_path / "live-voice.log", model_provider="openai", public_path="/simon"
        ) as api,
        sync_playwright() as pw,
    ):
        origin = str(api.base_url).rstrip("/")
        login = api.post(
            "/simon/auth/dev-login",
            headers={"Origin": origin},
            json={"token": "process-development-secret-32-characters"},
        )
        assert login.status_code == 200
        options = {
            "headless": True,
            "args": [
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--use-file-for-fake-audio-capture=" + str(silence),
            ],
        }
        if os.environ.get("SIMON_BROWSER_CHANNEL"):
            options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context(permissions=["microphone"])
            context.add_cookies(
                [
                    {
                        "name": "simon_session",
                        "value": api.cookies.get("simon_session"),
                        "url": origin,
                        "sameSite": "Strict",
                    }
                ]
            )
            context.add_init_script("""const NativePeer = RTCPeerConnection;
                window.RTCPeerConnection = class extends NativePeer {
                  constructor(...args) { super(...args); window.__livePeer = this; }
                };""")
            page = context.new_page()
            page.goto(origin + "/simon/chat")
            expect(page.locator("#connections-open")).to_be_enabled()
            page.locator("#voice-open").click()
            page.locator("#voice-start").click()
            try:
                page.wait_for_function(
                    '() => window.__livePeer?.connectionState === "connected"', timeout=55000
                )
                records = api.get("/simon/v1/voice").json()["sessions"]
                assert records[0]["state"] == "active"
            except Exception as exc:
                raise AssertionError(page.locator("#voice-status").inner_text()) from exc
            finally:
                if page.locator("#voice-end").is_enabled():
                    page.locator("#voice-end").click()
            records = api.get("/simon/v1/voice").json()["sessions"]
            identifier = records[0]["id"]
            # Closing waits for the provider's final usage acknowledgement.
            page.wait_for_function(
                '() => document.getElementById("voice-start").disabled === false'
            )
            api.post(
                "/simon/v1/voice/sessions/" + identifier + "/close",
                json={},
                headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
            )
            record = api.get("/simon/v1/voice/sessions/" + identifier).json()
            assert record["state"] == "closed" and record["usage_final"], record["error"]
            assert record["seconds"] >= 0
        finally:
            browser.close()
