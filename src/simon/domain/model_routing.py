"""Provider-neutral model inventory and explicit, reviewable routing decisions."""

from __future__ import annotations

import ipaddress
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from simon.domain.models import StrictModel

ModelProvider = Literal["openai_responses", "openai_compatible", "anthropic", "gemini"]
ModelTier = Literal["economy", "standard", "frontier"]


class ModelEndpoint(StrictModel):
    """Administrator-owned configuration; credentials are environment references only.

    Tier and capability declarations must reflect deployment evaluations. A local
    flag is a trust assertion about the deployment, not a model quality guarantee.
    Base URLs include their API version path (for example /v1 or /v1beta).
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    provider: ModelProvider
    model: str = Field(min_length=1, max_length=256)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    local: bool = False
    tier: ModelTier = "standard"
    capabilities: frozenset[str] = frozenset({"text"})
    context_window_tokens: int = Field(default=32768, ge=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    reasoning_efforts: tuple[str, ...] = ()
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    input_cost_per_million_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_cost_per_million_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError("Use an HTTP(S) base URL without credentials, query, or fragment")
        # Reading .port validates malformed or out-of-range ports as well.
        _ = parsed.port
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                raise ValueError("Unencrypted model endpoints must use a loopback address")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("Maximum output cannot exceed the context window")
        if not self.local and not self.api_key_env:
            raise ValueError("Cloud endpoints require an API-key environment reference")
        allowed = {
            "openai_responses": {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
                "ultra",
            },
            "openai_compatible": {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
                "ultra",
            },
            "anthropic": {"low", "medium", "high", "xhigh", "max"},
            "gemini": {"minimal", "low", "medium", "high"},
        }
        if not set(self.reasoning_efforts) <= allowed[self.provider]:
            raise ValueError("Reasoning effort is unsupported by this provider transport")
        if len(set(self.reasoning_efforts)) != len(self.reasoning_efforts):
            raise ValueError("Reasoning efforts must be unique")
        return self


class RoutingRequest(StrictModel):
    depth: int = Field(default=2, ge=1, le=5)
    importance: int = Field(default=2, ge=1, le=5)
    required_capabilities: frozenset[str] = frozenset({"text"})
    input_tokens: int = Field(default=2048, ge=0)
    output_tokens: int = Field(default=1024, ge=1)
    privacy: Literal["local_only", "allow_cloud"] = "allow_cloud"
    # Overrides name an inventory endpoint, never an arbitrary URL or model.
    model_override: str | None = Field(default=None, min_length=1, max_length=96)
    budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class EndpointRejection(StrictModel):
    endpoint_id: str
    reasons: tuple[str, ...]


class RoutingDecision(StrictModel):
    endpoint_id: str
    model: str
    provider: ModelProvider
    tier: ModelTier
    local: bool
    reasoning_effort: str | None = None
    reason: str
    estimated_cost_usd: float | None = None
    alternative_endpoint_ids: tuple[str, ...] = ()
    rejections: tuple[EndpointRejection, ...] = ()
    request: RoutingRequest


class TextGenerationRequest(StrictModel):
    """Bounded text generation. Tool loops and media have separate execution contracts."""

    prompt: str = Field(min_length=1, max_length=1_000_000)
    system: str = Field(default="", max_length=100_000)
    max_output_tokens: int = Field(default=1024, ge=1)


class TextGenerationResult(StrictModel):
    endpoint_id: str
    model: str
    text: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    # True means the API reported hitting its output limit; not an accepted deliverable.
    truncated: bool = False
