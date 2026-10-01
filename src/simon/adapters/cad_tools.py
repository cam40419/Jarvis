"""Lease-bound OpenSCAD export, mesh inspection and Blender studio rendering."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from simon.adapters._cad_runner import READ_OPERATIONS, WRITE_OPERATIONS, validate_arguments
from simon.adapters.environment_tools import LeasedCommandExecutor
from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentLease, ExecutionCommand
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

CAPABILITIES = frozenset({"python", "cad"})


@lru_cache(maxsize=1)
def _runner_source() -> str:
    return Path(__file__).with_name("_cad_runner.py").read_text(encoding="utf-8")


def cad_tool_definitions(*, enabled: bool = False) -> tuple[ToolDefinition, ...]:
    path = {"type": "string", "minLength": 1, "maxLength": 240}
    properties: dict[str, dict[str, Any]] = {
        "cad.openscad_export": {"output": path},
        "cad.mesh_inspect": {"vase_checks": {"type": "boolean"}},
        "cad.render_mesh": {
            "output": path, "material": {"type": "string", "enum": [
                "celadon", "ivory", "terracotta", "charcoal",
            ]}, "resolution": {"type": "integer", "minimum": 256, "maximum": 1600},
            "samples": {"type": "integer", "minimum": 8, "maximum": 128},
            "save_scene": {"type": "boolean"},
        },
    }
    descriptions = {
        "cad.openscad_export": "Export local parametric OpenSCAD source to a new STL or 3MF file.",
        "cad.mesh_inspect": "Inspect STL/3MF topology, dimensions and optional vase wall samples.",
        "cad.render_mesh": "Render STL/3MF as a studio PNG and optional editable Blender scene.",
    }
    return tuple(ToolDefinition(
        id=operation, description=descriptions[operation], transport="cad",
        categories=frozenset({"cad", "3d"}), capabilities=frozenset({operation}),
        enabled=enabled, configured=enabled,
        required_scopes=frozenset({"jobs:read" if operation in READ_OPERATIONS else "jobs:write"}),
        environment_capabilities=CAPABILITIES,
        side_effect=operation in WRITE_OPERATIONS,
        action_policy="write" if operation in WRITE_OPERATIONS else "read",
        settings={"network": False},
        input_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"input": path, **fields},
            "required": ["input", "output"] if operation in WRITE_OPERATIONS else ["input"],
        },
        output_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"exit_code": {"type": "integer"}, "stdout": {"type": "string"},
                           "stderr": {"type": "string"}, "truncated": {"type": "boolean"}},
            "required": ["exit_code", "stdout", "stderr", "truncated"],
        },
    ) for operation, fields in properties.items())


def cad_configuration_reason(definition: ToolDefinition) -> str | None:
    if definition.transport != "cad" or definition.id not in READ_OPERATIONS | WRITE_OPERATIONS:
        return "Unknown CAD operation"
    write = definition.id in WRITE_OPERATIONS
    if (definition.side_effect != write
            or definition.action_policy != ("write" if write else "read")
            or not {"jobs:write" if write else "jobs:read"} <= definition.required_scopes
            or not definition.environment_capabilities >= CAPABILITIES):
        return "CAD declaration requires its canonical action, scopes and capabilities"
    return None


class CadToolTransport:
    def __init__(
        self, manager: LeasedCommandExecutor, lease: EnvironmentLease, *,
        actor_id: UUID, run_id: UUID, max_timeout_seconds: int = 120,
        max_output_bytes: int = 65536,
    ) -> None:
        if (type(max_timeout_seconds) is not int or not 1 <= max_timeout_seconds <= 600
                or type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 1048576):
            raise ValueError("CAD limits are outside their supported range")
        if lease.status != "active":
            raise ToolCatalogError("CAD tools require an active lease")
        self.manager, self.lease = manager, lease.model_copy(deep=True)
        self.actor_id, self.run_id = actor_id, run_id
        self.max_timeout_seconds, self.max_output_bytes = max_timeout_seconds, max_output_bytes

    def __call__(
        self, definition: ToolDefinition, arguments: dict[str, Any], context: ToolExecutionContext,
    ) -> dict[str, Any]:
        assignment = self.lease.plan.request
        if (context.actor_id != self.actor_id or context.household_id != assignment.workspace_id
                or context.run_id != self.run_id or context.agent_id != assignment.agent_id):
            raise AuthorizationError("CAD does not belong to this worker lease")
        reason = cad_configuration_reason(definition)
        if reason or not definition.enabled or not definition.configured:
            raise ToolCatalogError(reason or "CAD tool is disabled or unconfigured")
        write = definition.id in WRITE_OPERATIONS
        if (definition.id not in context.allowed_tool_ids
                or not definition.required_scopes <= context.scopes
                or (write and context.authorized_action not in {"write", "external_commitment"})):
            raise AuthorizationError("CAD operation was not authorized")
        if (self.lease.definition.kind != "docker" or self.lease.definition.os != "linux"
                or self.lease.plan.network != "none" or self.lease.definition.network != "none"
                or not self.lease.definition.capabilities >= CAPABILITIES
                or not context.environment_capabilities >= CAPABILITIES
                or not definition.environment_capabilities <= self.lease.definition.capabilities
                or not definition.environment_capabilities <= context.environment_capabilities):
            raise ToolCatalogError("CAD requires an offline Linux Docker worker with its tools")
        try:
            checked = validate_arguments(definition.id, arguments)
        except ValueError as error:
            raise ToolCatalogError(str(error)) from None
        command = ExecutionCommand(
            argv=("/usr/local/bin/python3", "-I", "-c", _runner_source(), json.dumps({
                "operation": definition.id, "arguments": checked,
                "timeout_seconds": max(1, self.max_timeout_seconds - 2),
            })), timeout_seconds=self.max_timeout_seconds, max_output_bytes=self.max_output_bytes,
        )
        try:
            result = self.manager.execute(
                self.lease.id, command, attempt_id=assignment.attempt_id,
                fencing_token=self.lease.fencing_token,
            )
        except Exception:
            raise ToolExecutionError("Leased CAD command failed", unknown=write) from None
        if result.exit_code == 124:
            raise ToolExecutionError("Leased CAD command timed out", unknown=write)
        return result.model_dump(mode="json")
