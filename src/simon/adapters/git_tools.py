"""Structured local Git operations bound to an authorized Docker worker lease."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from simon.adapters._git_runner import validate_arguments
from simon.adapters.environment_tools import LeasedCommandExecutor
from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentLease, ExecutionCommand
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

GIT_CAPABILITIES = frozenset({"git", "python"})
_READ_OPERATIONS = frozenset({"status", "diff", "log", "branches"})
_OPERATIONS = (*sorted(_READ_OPERATIONS), "init", "branch", "switch", "add", "commit")


@lru_cache(maxsize=1)
def _runner_source() -> str:
    return Path(__file__).with_name("_git_runner.py").read_text(encoding="utf-8")


def git_tool_definitions(*, enabled: bool = False) -> tuple[ToolDefinition, ...]:
    """Ready-to-configure definitions; a compatible worker image is still required."""
    path = {"type": "string", "minLength": 1, "maxLength": 500}
    paths = {"type": "array", "items": path, "maxItems": 50}
    properties: dict[str, dict[str, Any]] = {
        "status": {},
        "branches": {},
        "diff": {"staged": {"type": "boolean"}, "paths": paths},
        "log": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        "init": {"branch": {"type": "string", "maxLength": 100}},
        "branch": {"name": {"type": "string", "maxLength": 100}},
        "switch": {"name": {"type": "string", "maxLength": 100}},
        "add": {"paths": {**paths, "minItems": 1}},
        "commit": {"message": {"type": "string", "minLength": 1, "maxLength": 4000}},
    }
    descriptions = {
        "status": "Inspect branch and working-tree changes in a leased local repository.",
        "diff": "Read a bounded staged or unstaged patch; paths are literal relative paths.",
        "log": "Read up to 100 recent commits from the current local branch.",
        "branches": "List local branches in a leased repository.",
        "init": "Create a local repository without replacing existing Git metadata.",
        "branch": "Create a local branch without switching or replacing an existing branch.",
        "switch": "Switch to an existing local branch; uncommitted conflicts are preserved.",
        "add": "Stage explicit relative paths in the leased local repository.",
        "commit": "Commit staged changes locally under the configured agent identity.",
    }
    return tuple(
        ToolDefinition(
            id="git." + operation,
            description=descriptions[operation],
            categories=frozenset({"software", "git"}),
            capabilities=frozenset({"git." + operation}),
            transport="git",
            enabled=enabled,
            configured=enabled,
            required_scopes=frozenset(
                {"jobs:read" if operation in _READ_OPERATIONS else "jobs:write"}
            ),
            environment_capabilities=GIT_CAPABILITIES,
            side_effect=operation not in _READ_OPERATIONS,
            action_policy="read" if operation in _READ_OPERATIONS else "write",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"repository": path, **properties[operation]},
                "required": (
                    {
                        "branch": ["name"],
                        "switch": ["name"],
                        "add": ["paths"],
                        "commit": ["message"],
                    }.get(operation, [])
                ),
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "exit_code": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "truncated": {"type": "boolean"},
                },
                "required": ["exit_code", "stdout", "stderr", "truncated"],
            },
            settings={"author_name": "Simon Agent", "author_email": "agent@simon.local"},
        )
        for operation in _OPERATIONS
    )


class GitToolTransport:
    def __init__(
        self,
        manager: LeasedCommandExecutor,
        lease: EnvironmentLease,
        *,
        actor_id: UUID,
        run_id: UUID,
        max_timeout_seconds: int = 60,
        max_output_bytes: int = 65536,
    ) -> None:
        if (
            type(max_timeout_seconds) is not int
            or not 1 <= max_timeout_seconds <= 600
            or type(max_output_bytes) is not int
            or not 1024 <= max_output_bytes <= 1048576
        ):
            raise ValueError("Git transport limits are outside their supported range")
        if lease.status != "active":
            raise ToolCatalogError("Git tools require an active lease")
        self.manager, self.lease = manager, lease.model_copy(deep=True)
        self.actor_id, self.run_id = actor_id, run_id
        self.max_timeout_seconds, self.max_output_bytes = max_timeout_seconds, max_output_bytes

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        assignment = self.lease.plan.request
        if (
            context.actor_id != self.actor_id
            or context.household_id != assignment.workspace_id
            or context.run_id != self.run_id
            or context.agent_id != assignment.agent_id
        ):
            raise AuthorizationError("Git execution does not belong to this worker lease")
        operation = definition.id.removeprefix("git.")
        if definition.id != "git." + operation or operation not in _OPERATIONS:
            raise ToolCatalogError("Unsupported Git operation")
        write = operation not in _READ_OPERATIONS
        scope = "jobs:write" if write else "jobs:read"
        if (
            definition.id not in context.allowed_tool_ids
            or scope not in context.scopes
            or not definition.required_scopes <= context.scopes
            or (write and context.authorized_action not in {"write", "external_commitment"})
        ):
            raise AuthorizationError("Git operation was not authorized")
        if (
            definition.transport != "git"
            or not definition.configured
            or not definition.enabled
            or definition.side_effect != write
            or definition.action_policy != ("write" if write else "read")
        ):
            raise ToolCatalogError("Git tool action declarations do not match its operation")
        if (
            self.lease.definition.kind != "docker"
            or self.lease.definition.os != "linux"
            or not self.lease.definition.capabilities >= GIT_CAPABILITIES
            or not context.environment_capabilities >= GIT_CAPABILITIES
            or not definition.environment_capabilities <= self.lease.definition.capabilities
            or not definition.environment_capabilities <= context.environment_capabilities
        ):
            raise ToolCatalogError("Git tools require a Linux Docker worker with Git and Python")
        try:
            checked = validate_arguments(operation, arguments)
        except ValueError as error:
            raise ToolCatalogError(str(error)) from None
        name = definition.settings.get("author_name", "Simon Agent")
        email = definition.settings.get("author_email", "agent@simon.local")
        if (
            not isinstance(name, str)
            or not isinstance(email, str)
            or not name.strip()
            or not email.strip()
            or len(name) > 100
            or len(email) > 200
            or any(ord(char) < 32 or char in "<>" for char in name + email)
        ):
            raise ToolCatalogError("Git author identity must be bounded plain text")
        command = ExecutionCommand(
            argv=(
                "/usr/local/bin/python3",
                "-I",
                "-c",
                _runner_source(),
                json.dumps(
                    {
                        "operation": operation,
                        "arguments": checked,
                        "author_name": name,
                        "author_email": email,
                        "timeout_seconds": max(1, self.max_timeout_seconds - 2),
                    }
                ),
            ),
            timeout_seconds=self.max_timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )
        try:
            result = self.manager.execute(
                self.lease.id,
                command,
                attempt_id=assignment.attempt_id,
                fencing_token=self.lease.fencing_token,
            )
        except Exception:
            raise ToolExecutionError("Leased Git command failed", unknown=write) from None
        if result.exit_code == 124:
            raise ToolExecutionError("Leased Git command timed out", unknown=write)
        return result.model_dump(mode="json")
