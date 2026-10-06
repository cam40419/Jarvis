"""Provider-neutral tool contracts. A catalog entry is not an installed integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, field_validator, model_validator

from simon.domain.errors import DomainError, ValidationError
from simon.domain.models import StrictModel, utc_now


class ToolCatalogError(ValidationError):
    code = "tool_catalog_error"


class ToolExecutionError(DomainError):
    code = "tool_execution_error"

    def __init__(self, message: str, *, unknown: bool = False) -> None:
        self.unknown = unknown
        super().__init__(message)


def _check_local_references(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise ValueError("Tool schemas only support document-local references")
            _check_local_references(child)
    elif isinstance(value, list):
        for child in value:
            _check_local_references(child)


def _check_nonsecret_settings(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "password",
                "secret",
                "token",
                "api_key",
                "authorization",
                "credentials",
                "access_token",
                "refresh_token",
                "private_key",
            }:
                raise ValueError("Tool credentials must use credential_env, not inline settings")
            _check_nonsecret_settings(child)
    elif isinstance(value, list):
        for child in value:
            _check_nonsecret_settings(child)


class ToolDefinition(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:/-]{0,199}$")
    description: str = Field(min_length=1, max_length=4000)
    categories: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    # An open string deliberately allows future application and execution protocols.
    transport: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    enabled: bool = True
    configured: bool = False
    required_scopes: frozenset[str] = frozenset()
    environment_capabilities: frozenset[str] = frozenset()
    side_effect: bool = False
    action_policy: Literal["read", "write", "external_commitment"] = "read"
    endpoint: str | None = None
    credential_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # Nonsecret transport configuration; credentials are resolved only via credential_env.
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def nonsecret_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        _check_nonsecret_settings(value)
        return value

    @field_validator("input_schema", "output_schema")
    @classmethod
    def valid_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("$schema", "https://json-schema.org/draft/2020-12/schema") not in {
            "https://json-schema.org/draft/2020-12/schema",
            "https://json-schema.org/draft/2020-12/schema#",
        }:
            raise ValueError("Tool schemas must use JSON Schema draft 2020-12")
        _check_local_references(value)
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError:
            raise ValueError("Invalid tool JSON schema") from None
        return value

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("Tool endpoints require HTTPS or loopback HTTP without credentials")
        return value

    @model_validator(mode="after")
    def consistent_action(self) -> ToolDefinition:
        if self.side_effect != (self.action_policy != "read"):
            raise ValueError("Tool side_effect and action_policy must agree")
        if self.transport == "http" and self.configured and not self.endpoint:
            raise ValueError("Configured HTTP tools require a fixed endpoint")
        return self


class ToolExecutionContext(StrictModel):
    actor_id: UUID
    workspace_id: UUID
    run_id: UUID
    agent_id: str = Field(min_length=1, max_length=200)
    invocation_id: UUID = Field(default_factory=uuid4)
    allowed_tool_ids: frozenset[str]
    scopes: frozenset[str] = frozenset()
    environment_capabilities: frozenset[str] = frozenset()
    # Supplied by the trusted action-policy layer after its own authorization checks.
    authorized_action: Literal["read", "write", "external_commitment"] = "read"


class ToolExecutionResult(StrictModel):
    tool_id: str
    actor_id: UUID
    workspace_id: UUID
    run_id: UUID
    agent_id: str
    invocation_id: UUID
    output: dict[str, Any]
    completed_at: datetime = Field(default_factory=utc_now)
