"""Lease-bound document conversion, OCR, PDF text, and media processing."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from simon.adapters._processing_runner import READ_OPERATIONS, WRITE_OPERATIONS, validate_arguments
from simon.adapters.environment_tools import LeasedCommandExecutor
from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentLease, ExecutionCommand
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

_CAPABILITIES = {
    "document.extract_pdf": "pdf",
    "document.convert": "documents",
    "image.ocr": "ocr",
    "media.inspect": "media",
    "media.thumbnail": "media",
    "media.extract_audio": "media",
    "media.transcode": "media",
}


@lru_cache(maxsize=1)
def _runner_source() -> str:
    return Path(__file__).with_name("_processing_runner.py").read_text(encoding="utf-8")


def processing_tool_definitions(*, enabled: bool = False) -> tuple[ToolDefinition, ...]:
    path = {"type": "string", "minLength": 1, "maxLength": 500}
    duration = {"type": "integer", "minimum": 1, "maximum": 600}
    width = {"type": "integer", "minimum": 64, "maximum": 1920, "multipleOf": 2}
    properties: dict[str, dict[str, Any]] = {
        "document.extract_pdf": {"max_pages": {"type": "integer", "minimum": 1, "maximum": 100}},
        "image.ocr": {},
        "media.inspect": {},
        "document.convert": {"output": path},
        "media.thumbnail": {
            "output": path,
            "width": width,
            "seconds": {"type": "integer", "minimum": 0, "maximum": 3600},
        },
        "media.extract_audio": {"output": path, "duration_seconds": duration},
        "media.transcode": {"output": path, "width": width, "duration_seconds": duration},
    }
    descriptions = {
        "document.extract_pdf": "Read bounded PDF text from a local worker file, up to 100 pages.",
        "document.convert": "Convert Markdown, text, or DOCX to a new Markdown/text/DOCX file.",
        "image.ocr": "Read English text from a local PNG, JPEG, or TIFF using OCR.",
        "media.inspect": "Read local audio/video stream metadata without contacting a network.",
        "media.thumbnail": "Create a PNG frame from local video at a bounded timestamp and width.",
        "media.extract_audio": "Create up to ten minutes of mono 16 kHz WAV for transcription.",
        "media.transcode": "Create up to ten minutes of H.264/AAC MP4 with a bounded width.",
    }
    return tuple(
        ToolDefinition(
            id=operation,
            description=descriptions[operation],
            transport="processing",
            categories=frozenset({operation.split(".")[0]}),
            capabilities=frozenset({operation}),
            enabled=enabled,
            configured=enabled,
            required_scopes=frozenset(
                {"jobs:read" if operation in READ_OPERATIONS else "jobs:write"}
            ),
            environment_capabilities=frozenset({"python", capability}),
            side_effect=operation in WRITE_OPERATIONS,
            action_policy="write" if operation in WRITE_OPERATIONS else "read",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"input": path, **properties[operation]},
                "required": ["input", "output"] if operation in WRITE_OPERATIONS else ["input"],
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
        )
        for operation, capability in _CAPABILITIES.items()
    )


class ProcessingToolTransport:
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
            raise ValueError("Processing transport limits are outside their supported range")
        if lease.status != "active":
            raise ToolCatalogError("Processing tools require an active lease")
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
            raise AuthorizationError("Processing does not belong to this worker lease")
        operation = definition.id
        if operation not in _CAPABILITIES:
            raise ToolCatalogError("Unsupported processing operation")
        write = operation in WRITE_OPERATIONS
        scope = "jobs:write" if write else "jobs:read"
        if (
            definition.id not in context.allowed_tool_ids
            or scope not in context.scopes
            or not definition.required_scopes <= context.scopes
            or (write and context.authorized_action not in {"write", "external_commitment"})
        ):
            raise AuthorizationError("Processing operation was not authorized")
        if (
            definition.transport != "processing"
            or not definition.enabled
            or not definition.configured
            or definition.side_effect != write
            or definition.action_policy != ("write" if write else "read")
        ):
            raise ToolCatalogError("Processing declarations do not match the operation")
        required = frozenset({"python", _CAPABILITIES[operation]})
        if (
            self.lease.definition.kind != "docker"
            or self.lease.definition.os != "linux"
            or self.lease.plan.network != "none"
            or self.lease.definition.network != "none"
            or not required <= self.lease.definition.capabilities
            or not required <= context.environment_capabilities
            or not definition.environment_capabilities <= self.lease.definition.capabilities
            or not definition.environment_capabilities <= context.environment_capabilities
        ):
            raise ToolCatalogError("Processing requires an offline Docker worker with its tools")
        try:
            checked = validate_arguments(operation, arguments)
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
                        "operation": operation,
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
            raise ToolExecutionError("Leased processing command failed", unknown=write) from None
        if result.exit_code == 124:
            raise ToolExecutionError("Leased processing command timed out", unknown=write)
        return result.model_dump(mode="json")
