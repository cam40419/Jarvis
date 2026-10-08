"""Bounded HTTP and explicit tenant grants for operator-managed service accounts."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)


def relative_path(value: Any, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 1000
        or (not value and not empty)
        or value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 or char in "\\%?#:" for char in value)
        or (any(part in {".", "..", ""} for part in value.split("/")) and value)
    ):
        raise ToolCatalogError("Paths must be plain relative paths inside the configured root")
    return value


def connection_endpoint(definition: ToolDefinition) -> str:
    endpoint = definition.endpoint
    if not endpoint:
        raise ToolCatalogError("The optional integration has no configured endpoint")
    parsed = urlsplit(endpoint)
    # Revalidate deep-copied definitions and reject encoded traversal in operator roots too.
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
        or any(ord(char) < 32 or ord(char) == 127 for char in endpoint)
    ):
        raise ToolCatalogError("Integration endpoints require HTTPS or loopback HTTP")
    decoded = unquote(parsed.path).strip("/")
    relative_path(decoded, empty=True)
    return endpoint.rstrip("/")


def tenant_grant(definition: ToolDefinition, workspace_id: UUID, actor_id: UUID) -> None:
    configured_workspace = definition.settings.get("workspace_id")
    actor_ids = definition.settings.get("actor_ids")
    try:
        if not isinstance(actor_ids, list) or not actor_ids or len(actor_ids) > 100:
            raise ValueError
        workspace = UUID(str(configured_workspace))
        actors = {UUID(str(value)) for value in actor_ids}
    except (ValueError, TypeError, AttributeError):
        raise ToolCatalogError(
            "Configure one workspace and an explicit actor allowlist for this service account"
        ) from None
    if workspace_id != workspace or actor_id not in actors:
        raise AuthorizationError("This service account was not granted to this workspace and actor")


def authorize_operation(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
    *,
    transport: str,
    write: bool,
    schema: dict[str, Any],
) -> None:
    scope = "jobs:write" if write else "jobs:read"
    if (
        definition.id not in context.allowed_tool_ids
        or scope not in context.scopes
        or not definition.required_scopes <= context.scopes
        or (write and context.authorized_action not in {"write", "external_commitment"})
    ):
        raise AuthorizationError("The optional tool operation was not authorized")
    if (
        definition.transport != transport
        or not definition.enabled
        or not definition.configured
        or definition.side_effect != write
        or definition.action_policy != ("write" if write else "read")
    ):
        raise ToolCatalogError("Optional tool configuration does not match this operation")
    tenant_grant(definition, context.workspace_id, context.actor_id)
    try:
        # Canonical operation schemas cannot be widened by a manifest edit.
        Draft202012Validator(schema).validate(arguments)
    except ValidationError:
        raise ToolCatalogError("Optional tool arguments do not match the operation") from None


def credential(definition: ToolDefinition, environ: Mapping[str, str]) -> str:
    value = environ.get(definition.credential_env or "", "").strip()
    if not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ToolCatalogError("The optional integration credential is unavailable")
    return value


def redact(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


def redact_bytes(value: bytes, secrets: Sequence[str]) -> tuple[bytes, bool]:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret.encode("utf-8"), b"[redacted]")
    return result, result != value


@dataclass(frozen=True)
class HTTPResult:
    content: bytes
    headers: httpx.Headers
    status: int

    def json(self, *, write: bool) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeError, RecursionError):
            raise ToolExecutionError(
                "The remote service returned invalid JSON", unknown=write
            ) from None


class BoundedHTTP:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if not 0 < timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 2_000_000:
            raise ValueError("Optional HTTP limits are outside their supported range")
        self.transport = transport
        self.environ = os.environ if environ is None else environ
        self.timeout_seconds, self.max_response_bytes = timeout_seconds, max_response_bytes

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        expected: frozenset[int],
        write: bool = False,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> HTTPResult:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with httpx.Client(  # noqa: SIM117
                transport=self.transport,
                timeout=self.timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    method, url, headers=headers, content=content, params=params
                ) as response:
                    if response.status_code not in expected:
                        if response.status_code == 412:
                            raise ToolExecutionError(
                                "The remote file changed or already exists; read its latest ETag"
                            )
                        raise ToolExecutionError(
                            f"Remote service rejected the request (HTTP {response.status_code})",
                            unknown=write
                            and response.status_code
                            not in {
                                400,
                                401,
                                403,
                                404,
                                405,
                                409,
                                410,
                                412,
                                413,
                                415,
                                422,
                                423,
                                429,
                            },
                        )
                    data = bytearray()
                    for part in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise ToolExecutionError(
                                "The optional integration response timed out",
                                unknown=write,
                            )
                        data.extend(part)
                        if len(data) > self.max_response_bytes:
                            raise ToolExecutionError(
                                "The remote response exceeded the size limit",
                                unknown=write,
                            )
                    return HTTPResult(bytes(data), response.headers, response.status_code)
        except (httpx.HTTPError, ValueError):
            raise ToolExecutionError(
                "The optional integration connection failed",
                unknown=write,
            ) from None


def optional_tool_status(
    definition: ToolDefinition,
    actor: ActorContext,
    environ: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    """Local preflight only; a configured service has not been probed on the network."""
    if not definition.enabled:
        return "disabled", ("This optional integration is disabled",)
    if not definition.configured:
        return "unconfigured", ("This optional integration has not been configured",)
    try:
        tenant_grant(definition, actor.workspace_id, actor.actor_id)
        connection_endpoint(definition)
        credential(definition, environ)
        if definition.transport == "github":
            repositories = definition.settings.get("repositories")
            if (
                not isinstance(repositories, list)
                or not repositories
                or len(repositories) > 100
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(
                        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                        item,
                    )
                    is None
                    for item in repositories
                )
            ):
                raise ToolCatalogError("Configure an explicit GitHub repository allowlist")
            for repository in repositories:
                relative_path(repository)
        if definition.transport == "webdav":
            username = definition.settings.get("username")
            if (
                not isinstance(username, str)
                or not username
                or len(username) > 200
                or any(ord(char) < 32 or char == ":" for char in username)
            ):
                raise ToolCatalogError("Configure a WebDAV service account username")
    except AuthorizationError as error:
        return "permission_required", (str(error),)
    except ToolCatalogError as error:
        return "unconfigured", (str(error),)
    return "configured", ()
