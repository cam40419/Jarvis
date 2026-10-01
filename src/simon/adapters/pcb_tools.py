"""KiCad checks and exports inside one authorized offline Docker worker lease."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from simon.adapters._pcb_runner import OPERATIONS, validate_arguments
from simon.adapters.environment_tools import LeasedCommandExecutor
from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentLease, ExecutionCommand
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

PCB_CAPABILITIES = frozenset({"python", "pcb"})


@lru_cache(maxsize=1)
def _runner_source() -> str:
    return Path(__file__).with_name("_pcb_runner.py").read_text(encoding="utf-8")


def pcb_tool_definitions(*, enabled: bool = False) -> tuple[ToolDefinition, ...]:
    descriptions = {
        "pcb.erc": "Run KiCad electrical rule checks and save JSON, including violations.",
        "pcb.drc": "Check PCB design rules and schematic parity; save JSON including violations.",
        "pcb.schematic_pdf": "Export a KiCad schematic to a reviewable PDF.",
        "pcb.netlist": "Export KiCad schematic connectivity as XML netlist.",
        "pcb.bom": "Export schematic references, values, footprints and quantities as CSV.",
        "pcb.board_svg": "Export PCB top copper, silkscreen and board outline as SVG.",
        "pcb.gerbers": "Export Gerber layers and Excellon drills into a ZIP for review.",
    }
    path = {"type": "string", "minLength": 1, "maxLength": 500}
    return tuple(
        ToolDefinition(
            id=operation,
            description=description,
            transport="pcb",
            enabled=enabled,
            configured=enabled,
            categories=frozenset({"hardware", "pcb"}),
            capabilities=frozenset({operation}),
            required_scopes=frozenset({"jobs:write"}),
            environment_capabilities=PCB_CAPABILITIES,
            side_effect=True,
            action_policy="write",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"input": path, "output": path},
                "required": ["input", "output"],
            },
        )
        for operation, description in descriptions.items()
    )


def pcb_configuration_reason(definition: ToolDefinition) -> str | None:
    if definition.transport != "pcb" or definition.id not in OPERATIONS:
        return "Unknown PCB operation"
    if (
        not definition.side_effect
        or definition.action_policy != "write"
        or not {"jobs:write"} <= definition.required_scopes
        or not definition.environment_capabilities >= PCB_CAPABILITIES
    ):
        return "PCB declaration requires its canonical action, scopes and capabilities"
    return None


class PCBToolTransport:
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
            raise ValueError("PCB transport limits are outside their supported range")
        if lease.status != "active":
            raise ToolCatalogError("PCB tools require an active lease")
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
            raise AuthorizationError("PCB operation does not belong to this worker lease")
        if (
            definition.id not in context.allowed_tool_ids
            or "jobs:write" not in context.scopes
            or not definition.required_scopes <= context.scopes
            or context.authorized_action not in {"write", "external_commitment"}
        ):
            raise AuthorizationError("PCB operation was not authorized")
        reason = pcb_configuration_reason(definition)
        if reason:
            raise ToolCatalogError(reason)
        if not definition.enabled or not definition.configured:
            raise ToolCatalogError("PCB declarations do not match the operation")
        if (
            self.lease.definition.kind != "docker"
            or self.lease.definition.os != "linux"
            or self.lease.plan.network != "none"
            or self.lease.definition.network != "none"
            or not self.lease.definition.capabilities >= PCB_CAPABILITIES
            or not context.environment_capabilities >= PCB_CAPABILITIES
            or not definition.environment_capabilities <= self.lease.definition.capabilities
            or not definition.environment_capabilities <= context.environment_capabilities
        ):
            raise ToolCatalogError("PCB operations require an offline KiCad Docker worker")
        try:
            checked = validate_arguments(definition.id, arguments)
        except ValueError as error:
            raise ToolCatalogError(str(error)) from None
        command = ExecutionCommand(
            argv=(
                "/usr/local/bin/python3",
                "-I",
                "-c",
                _runner_source(),
                json.dumps(
                    {
                        "operation": definition.id,
                        "arguments": checked,
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
            raise ToolExecutionError("Leased PCB command failed", unknown=True) from None
        if result.exit_code == 124:
            raise ToolExecutionError("Leased PCB command timed out", unknown=True)
        return result.model_dump(mode="json")
