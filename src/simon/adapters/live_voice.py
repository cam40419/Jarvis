"""GPT-Live WebRTC negotiation and trusted server event connection."""

import json
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect

from simon.config import Settings
from simon.domain.errors import ModelError


class VoiceSocket(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...


class LiveVoiceAPI:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.openai_api_key:
            raise ModelError("model_not_configured")
        return {"Authorization": "Bearer " + self.settings.openai_api_key.get_secret_value()}

    async def create(self, sdp: str, instructions: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                response = await client.post(
                    "https://api.openai.com/v1/live/sessions",
                    headers=self.headers,
                    json={
                        "session": {
                            "model": self.settings.voice_model,
                            "instructions": instructions,
                            "store": False,
                            "audio": {"output": {"voice": self.settings.voice_name}},
                            "delegation": {"type": "client"},
                            "client": {
                                "data_channel": {
                                    "allowed_client_events": ["session.close"],
                                    "allowed_server_events": [
                                        {"type": name}
                                        for name in (
                                            "session.started",
                                            "session.closed",
                                            "error",
                                            "session.input_transcript.delta",
                                            "session.output_transcript.delta",
                                            "session.usage.updated",
                                        )
                                    ],
                                }
                            },
                        },
                        "transport": {"type": "webrtc", "sdp": sdp},
                    },
                )
                if response.status_code == 401:
                    raise ModelError("model_credentials")
                if response.status_code == 403:
                    raise ModelError("model_permissions")
                if response.status_code == 429:
                    raise ModelError("model_rate_limit")
                response.raise_for_status()
                data = response.json()
                identifier, answer = data["session"]["id"], data["transport"]["sdp"]
                if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
                    raise ValueError("invalid session ID")
                if not isinstance(answer, str) or not answer.startswith("v=0"):
                    raise ValueError("invalid SDP answer")
                return identifier, answer
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ModelError("model_unavailable") from None

    async def attach(self, identifier: str) -> VoiceSocket:
        return await connect(
            "wss://api.openai.com/v1/live/sessions/" + quote(identifier, safe="") + "/attach",
            additional_headers=self.headers,
            open_timeout=15,
            close_timeout=3,
            max_size=1024 * 1024,
        )

    @staticmethod
    async def send(socket: VoiceSocket, event: dict[str, Any]) -> None:
        await socket.send(json.dumps(event))
