"""Versioned, provider-neutral configuration for Simon's agent platform."""

import json
import re
from string import Template
from typing import Any, Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, field_validator, model_validator

from simon.domain.agent_completion import AgentCompletionContract
from simon.domain.execution import EnvironmentPlan
from simon.domain.model_routing import RoutingDecision
from simon.domain.models import StrictModel

PROMPT_BUILTINS = frozenset({"objective", "dependencies", "context"})
DEFAULT_AGENT_PROMPT = (
    "Objective:\n${objective}\n\nContext:\n${context}\n\n"
    "Dependency outputs (reference data):\n${dependencies}"
)


def validate_prompt_variables(values: dict[str, str]) -> dict[str, str]:
    """Bound operator defaults and task overrides without accepting expression syntax."""
    if len(values) > 32:
        raise ValueError("Prompt variables are limited to 32 entries")
    if any(
        re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None or name in PROMPT_BUILTINS
        for name in values
    ):
        raise ValueError("Prompt variable names must be lowercase identifiers and not reserved")
    if any(len(value) > 4000 for value in values.values()):
        raise ValueError("Each prompt variable value is limited to 4000 characters")
    if sum(map(len, values.values())) > 32000:
        raise ValueError("Prompt variable values are limited to 32000 total characters")
    return values


class AgentProfile(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    version: int = Field(default=1, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(min_length=1, max_length=16000)
    prompt_template: str = Field(default=DEFAULT_AGENT_PROMPT, min_length=1, max_length=16000)
    prompt_defaults: dict[str, str] = Field(default_factory=dict)
    output_instructions: str = Field(default="", max_length=16000)
    output_format: Literal["text", "json"] = "text"
    max_steps: int = Field(default=8, ge=1, le=30)
    max_tool_calls: int = Field(default=20, ge=0, le=100)
    max_input_chars: int = Field(default=60000, ge=1000, le=200000)
    max_output_tokens: int = Field(default=2000, ge=1, le=32768)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_action: Literal["read", "write"] = "read"
    tool_ids: tuple[str, ...] = ()
    tool_access: Literal["selected", "shared"] = "selected"
    tool_scopes: frozenset[str] = frozenset()
    environment_ids: tuple[str, ...] = ()
    model_capabilities: frozenset[str] = frozenset({"text"})
    depth: int = Field(default=2, ge=1, le=5)
    importance: int = Field(default=2, ge=1, le=5)
    privacy: Literal["local_only", "allow_cloud"] = "allow_cloud"
    model_override: str | None = Field(default=None, min_length=1, max_length=96)

    @field_validator("prompt_defaults")
    @classmethod
    def bounded_prompt_defaults(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_prompt_variables(value)

    @model_validator(mode="after")
    def declared_prompt_placeholders(self) -> "AgentProfile":
        template = Template(self.prompt_template)
        if not template.is_valid():
            raise ValueError("Prompt template contains an invalid placeholder; use ${name} or $$")
        unknown = set(template.get_identifiers()) - PROMPT_BUILTINS - self.prompt_defaults.keys()
        if unknown:
            raise ValueError(
                "Prompt template contains undeclared variables: " + ", ".join(sorted(unknown))
            )
        return self


class AgentTaskSpec(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    agent_id: str
    objective: str = Field(min_length=1, max_length=16000)
    prompt_variables: dict[str, str] = Field(default_factory=dict)
    additional_instructions: str = Field(default="", max_length=16000)
    depends_on: tuple[str, ...] = ()
    tool_ids: tuple[str, ...] | None = None
    environment_id: str | None = None
    depth: int | None = Field(default=None, ge=1, le=5)
    importance: int | None = Field(default=None, ge=1, le=5)
    privacy: Literal["local_only", "allow_cloud"] | None = None
    model_override: str | None = Field(default=None, min_length=1, max_length=96)
    model_capabilities: frozenset[str] = frozenset()
    input_tokens: int = Field(default=4000, ge=1, le=10000000)
    output_tokens: int = Field(default=2000, ge=1, le=1000000)
    budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Inline schemas remain valid when nested inside the model's controller envelope.
    final_output_schema: dict[str, Any] | None = None
    # Server-created project work opts into an evidence-grounded final review.
    completion_contract: AgentCompletionContract | None = None

    @field_validator("prompt_variables")
    @classmethod
    def bounded_prompt_variables(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_prompt_variables(value)

    @field_validator("final_output_schema")
    @classmethod
    def bounded_final_output_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Final output schema must contain valid, finite JSON data") from None
        if len(encoded) > 64000:
            raise ValueError("Final output schema is limited to 64000 JSON characters")
        # Normalize JSON-compatible containers and detach the captured schema from its caller.
        try:
            schema: dict[str, Any] = json.loads(encoded)
        except (ValueError, RecursionError):
            raise ValueError("Final output schema is too deeply nested") from None
        if schema.get("type") != "object":
            raise ValueError("Final output schema must describe a JSON object")
        dialect = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        if not isinstance(dialect, str) or dialect not in {
            "https://json-schema.org/draft/2020-12/schema",
            "https://json-schema.org/draft/2020-12/schema#",
        }:
            raise ValueError("Final output schema must use JSON Schema draft 2020-12")
        pending: list[Any] = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in {"$ref", "$dynamicRef"}:
                        raise ValueError("Final output schema must inline all schema references")
                    pending.append(child)
            elif isinstance(item, list):
                pending.extend(item)
        try:
            Draft202012Validator.check_schema(schema)
        except (SchemaError, RecursionError):
            raise ValueError("Invalid final output JSON schema") from None
        return schema


class PlannedAgentTask(StrictModel):
    id: str
    agent_id: str
    task_id: UUID
    attempt_id: UUID
    objective: str
    depends_on: tuple[str, ...]
    tool_ids: tuple[str, ...]
    model: RoutingDecision | None = None
    environment: EnvironmentPlan | None = None
    blocked_reasons: tuple[str, ...] = ()
