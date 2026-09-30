"""Optional RobbinsHome HTTP integration. No hardware or shared database access."""

from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from simon.adapters.google import ConnectedError
from simon.config import Settings
from simon.domain.connected_tools import ToolName
from simon.domain.external_home import HomeCommand
from simon.domain.models import ActorContext

TOOL_SCOPES = {
    "home_list_devices": {"home:read"},
    "home_get_status": {"home:read"},
    "home_get_statuses": {"home:read"},
    "home_refresh_devices": {"home:read"},
    "home_control": {"home:read", "home:control"},
    "home_organize_devices": {"home:read", "home:organize"},
    "home_rename_device": {"home:read", "home:organize"},
    "home_setup_outlet": {"home:read", "identity:manage"},
    "display_list": {"home:read"},
    "display_configure": {"home:read", "identity:manage"},
}


class HomeClient:
    def __init__(self, settings: Settings) -> None:
        self.url = settings.home_api_url.rstrip("/")
        self.token = settings.home_api_token
        if self.url:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme == "http"
                    and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError("home_api_url requires HTTPS or loopback HTTP without credentials")

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token and self.token.get_secret_value().strip())

    def available(self, actor: ActorContext) -> tuple[ToolName, ...]:
        if not self.configured:
            return ()
        return tuple(
            cast(ToolName, name) for name, scopes in TOOL_SCOPES.items() if scopes <= actor.scopes
        )

    def request(self, actor: ActorContext, path: str, body: dict[str, Any]) -> Any:
        if not self.configured or self.token is None:
            raise ConnectedError("RobbinsHome is not connected.")
        try:
            # No automatic retry, proxy inheritance, or redirects, especially for writes.
            with httpx.Client(  # noqa: SIM117
                timeout=8 if path.endswith("/commands") else 60,
                trust_env=False, follow_redirects=False,
            ) as client:
                with client.stream(
                    "POST",
                    self.url + path,
                    json=body,
                    headers={
                        "Authorization": "Bearer " + self.token.get_secret_value(),
                        "X-Actor-ID": str(actor.actor_id),
                        "X-Household-ID": str(actor.household_id),
                    },
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ConnectedError(
                            "RobbinsHome rejected the request. "
                            "Check its connection and permissions.",
                            unknown=response.status_code >= 500 or response.status_code == 408,
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 2_000_000:
                            raise ConnectedError(
                                "RobbinsHome response exceeded its limit.", unknown=True
                            )
                    import json

                    return json.loads(content)
        except (httpx.HTTPError, ValueError):
            raise ConnectedError(
                "RobbinsHome is unavailable or returned an invalid response. A submitted command "
                "may have executed; check its history before requesting another change.",
                unknown=True,
            ) from None

    def execute(
        self,
        actor: ActorContext,
        name: str,
        arguments: dict[str, Any],
        *,
        run_id: UUID,
        thread_id: UUID,
    ) -> Any:
        if name not in self.available(actor):
            raise ConnectedError("Home tool access is unavailable.")
        return self.request(
            actor,
            "/v1/tools/execute",
            {
                "name": name,
                "arguments": arguments,
                "run_id": str(run_id),
                "thread_id": str(thread_id),
            },
        )

    def commands(
        self, actor: ActorContext, run_id: UUID | None = None, *, thread_id: UUID | None = None
    ) -> tuple[HomeCommand, ...]:
        if "home:read" not in actor.scopes:
            return ()
        try:
            rows = self.request(
                actor,
                "/v1/tools/commands",
                {
                    "run_id": str(run_id) if run_id else None,
                    "thread_id": str(thread_id) if thread_id else None,
                },
            )
            return tuple(
                HomeCommand.model_validate(row)
                for row in rows
                if row.get("actor_id") == str(actor.actor_id)
                and row.get("household_id") == str(actor.household_id)
                and (run_id is None or row.get("run_id") == str(run_id))
                and (thread_id is None or row.get("thread_id") == str(thread_id))
            )
        except (ConnectedError, ValueError, TypeError, AttributeError):
            # An offline home service must not make assistant conversation history unavailable.
            return ()
