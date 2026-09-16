import asyncio
import json
from functools import partial

import httpx
import pytest
from pydantic import SecretStr

from simon.adapters.live_voice import LiveVoiceAPI
from simon.config import Settings
from simon.domain.errors import ModelError


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "model_credentials"),
        (403, "model_permissions"),
        (429, "model_rate_limit"),
        (500, "model_unavailable"),
    ],
)
def test_voice_provider_errors_are_sanitized(monkeypatch, status, reason):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(
            httpx.AsyncClient,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="secret provider detail")
            ),
        ),
    )
    api = LiveVoiceAPI(Settings(_env_file=None, openai_api_key=SecretStr("fake")))
    with pytest.raises(ModelError) as error:
        asyncio.run(api.create("v=0", "instructions"))
    assert error.value.reason == reason and "secret" not in str(error.value)


def test_voice_negotiation_restricts_browser_and_keeps_key_server_side(monkeypatch):
    def handler(request):
        assert request.url == "https://api.openai.com/v1/live/sessions"
        assert request.headers["authorization"] == "Bearer fake"
        body = json.loads(request.content)
        assert body["transport"] == {"type": "webrtc", "sdp": "v=0"}
        config = body["session"]
        assert config["model"] == "gpt-live-1" and not config["store"]
        assert config["delegation"] == {"type": "client"}
        assert config["client"]["data_channel"]["allowed_client_events"] == ["session.close"]
        return httpx.Response(
            201, json={"session": {"id": "live_opaque"}, "transport": {"sdp": "v=0\r\nanswer"}}
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    )
    api = LiveVoiceAPI(Settings(_env_file=None, openai_api_key=SecretStr("fake")))
    assert asyncio.run(api.create("v=0", "instructions")) == ("live_opaque", "v=0\r\nanswer")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session": {"id": 4}, "transport": {"sdp": "v=0"}},
        {"session": {"id": "live_x"}, "transport": {"sdp": "invalid"}},
    ],
)
def test_voice_invalid_answer(monkeypatch, body):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(
            httpx.AsyncClient,
            transport=httpx.MockTransport(lambda _: httpx.Response(201, json=body)),
        ),
    )
    api = LiveVoiceAPI(Settings(_env_file=None, openai_api_key=SecretStr("fake")))
    with pytest.raises(ModelError):
        asyncio.run(api.create("v=0", "instructions"))


def test_voice_sideband_encodes_opaque_id(monkeypatch):
    async def connect(url, **kwargs):
        assert url == "wss://api.openai.com/v1/live/sessions/live_%2F%3F/attach"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer fake"
        return "socket"

    monkeypatch.setattr("simon.adapters.live_voice.connect", connect)
    api = LiveVoiceAPI(Settings(_env_file=None, openai_api_key=SecretStr("fake")))
    assert asyncio.run(api.attach("live_/?")) == "socket"
    api.settings.openai_api_key = None
    with pytest.raises(ModelError):
        _ = api.headers
