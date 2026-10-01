from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from simon.adapters._processing_runner import (
    processing_command,
    validate_arguments,
    workspace_file,
)
from simon.adapters.processing_tools import ProcessingToolTransport, processing_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import AuthorizationError
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentRequest,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)
from simon.domain.models import utc_now
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)


class RecordingManager:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, ExecutionCommand, UUID, int]] = []
        self.fail = False
        self.result = ExecutionResult(exit_code=0, stdout="Extracted text")

    def execute(
        self, lease_id: UUID, command: ExecutionCommand, *, attempt_id: UUID, fencing_token: int,
    ) -> ExecutionResult:
        self.calls.append((lease_id, command, attempt_id, fencing_token))
        if self.fail:
            raise ExecutionError("sensitive detail")
        return self.result


def setup(operation: str = "document.extract_pdf") -> tuple[
    RecordingManager, EnvironmentLease, ToolDefinition, ToolExecutionContext,
    ProcessingToolTransport,
]:
    tool = next(item for item in processing_tool_definitions(enabled=True) if item.id == operation)
    definition = EnvironmentDefinition(
        id="processor", kind="docker", container_image="simon-processing:local", enabled=True,
        capabilities=frozenset({"python", "pdf", "ocr", "media", "documents"}),
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(), agent_id="processor", task_id=uuid4(), attempt_id=uuid4(),
        capabilities=tool.environment_capabilities,
    )
    lease = EnvironmentLease(
        id=uuid4(), plan=EnvironmentPlan(
            environment_id=definition.id, kind="docker", request=request,
            workspace_path=Path("unused"), network="none", cpu_limit=2, memory_mb=2048,
            gpu_devices=(),
        ), definition=definition, fencing_token=4, status="active", resource_handle="owned",
        created_at=utc_now(), heartbeat_at=utc_now(),
    )
    context = ToolExecutionContext(
        actor_id=uuid4(), household_id=request.workspace_id, run_id=uuid4(), agent_id="processor",
        allowed_tool_ids=frozenset({tool.id}), scopes=tool.required_scopes,
        environment_capabilities=definition.capabilities, authorized_action=tool.action_policy,
    )
    manager = RecordingManager()
    transport = ProcessingToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id,
    )
    return manager, lease, tool, context, transport


def test_definitions_are_disabled_until_image_is_provisioned() -> None:
    definitions = processing_tool_definitions()
    assert len(definitions) == 7
    assert all(not item.enabled and not item.configured for item in definitions)


def test_processing_executes_guard_inside_exact_lease() -> None:
    manager, lease, tool, context, transport = setup()
    registry = TransportRegistry()
    registry.register("processing", transport)
    result = registry.execute(tool, {"input": "report.pdf"}, context)
    assert result.output["stdout"] == "Extracted text"
    identifier, command, attempt, fence = manager.calls[0]
    assert (identifier, attempt, fence) == (lease.id, lease.plan.request.attempt_id, 4)
    assert command.argv[:3] == ("/usr/local/bin/python3", "-I", "-c")
    payload = json.loads(command.argv[4])
    assert payload["arguments"] == {"input": "report.pdf", "max_pages": 20}
    assert command.max_output_bytes == 65536


@pytest.mark.parametrize("field", ["actor_id", "household_id", "run_id", "agent_id"])
def test_cannot_cross_worker_assignment(field: str) -> None:
    manager, _, tool, context, transport = setup()
    value: Any = "other" if field == "agent_id" else uuid4()
    with pytest.raises(AuthorizationError, match="worker lease"):
        transport(tool, {"input": "report.pdf"}, context.model_copy(update={field: value}))
    assert manager.calls == []


@pytest.mark.parametrize("change", [
    {"scopes": frozenset()}, {"allowed_tool_ids": frozenset()}, {"authorized_action": "read"},
])
def test_conversion_requires_write_authorization(change: dict[str, Any]) -> None:
    manager, _, tool, context, transport = setup("document.convert")
    with pytest.raises(AuthorizationError):
        transport(tool, {"input": "report.md", "output": "report.docx"},
                  context.model_copy(update=change))
    assert manager.calls == []


def test_online_environment_is_rejected() -> None:
    manager, lease, tool, context, _ = setup()
    lease = lease.model_copy(update={"definition": lease.definition.model_copy(
        update={"network": "bridge"},
    )})
    transport = ProcessingToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id,
    )
    with pytest.raises(ToolCatalogError, match="offline"):
        transport(tool, {"input": "report.pdf"}, context)
    assert manager.calls == []


@pytest.mark.parametrize("path", ["../report.pdf", "/tmp/report.pdf", "C:/report.pdf",
                                  "https://example.test/report.pdf", ".git/report.pdf",
                                  "--report.pdf", "report\x00.pdf"])
def test_bad_paths_rejected_before_runner(path: str) -> None:
    manager, _, tool, context, transport = setup()
    with pytest.raises(ToolCatalogError):
        transport(tool, {"input": path}, context)
    assert manager.calls == []


@pytest.mark.parametrize(("operation", "arguments"), [
    ("document.extract_pdf", {"input": "report.pdf", "max_pages": 101}),
    ("document.extract_pdf", {"input": "report.pdf", "max_pages": True}),
    ("document.extract_pdf", {"input": "report.html"}),
    ("document.convert", {"input": "report.md", "output": "report.pdf"}),
    ("document.convert", {"input": "report.md", "output": "report.md"}),
    ("image.ocr", {"input": "source.svg"}),
    ("media.inspect", {"input": "remote.m3u8"}),
    ("media.transcode", {"input": "video.mp4", "output": "new.mp4", "width": 101}),
    ("media.transcode", {"input": "video.mp4", "output": "new.mp4", "duration_seconds": 601}),
    ("media.inspect", {"input": "video.mp4", "args": ["-show_data"]}),
])
def test_options_formats_and_limits_are_fixed(operation: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_arguments(operation, arguments)


def test_input_and_output_guards_preserve_existing_files(tmp_path: Path) -> None:
    existing = tmp_path / "report.pdf"
    existing.write_bytes(b"fixture")
    assert workspace_file(tmp_path, "report.pdf") == existing
    with pytest.raises(ValueError, match="already exists"):
        workspace_file(tmp_path, "report.pdf", output=True)
    assert existing.read_bytes() == b"fixture"
    with pytest.raises(ValueError, match="regular file"):
        workspace_file(tmp_path, "missing.pdf")


def test_symlink_input_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "report.pdf"
    target.write_bytes(b"fixture")
    link = tmp_path / "linked.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink privileges unavailable")
    with pytest.raises(ValueError, match="Symbolic links"):
        workspace_file(tmp_path, "linked.pdf")


def test_pandoc_uses_sandbox_and_fixed_formats() -> None:
    source, output = Path("/workspace/source.md"), Path("/workspace/output.docx")
    arguments = validate_arguments("document.convert", {"input": "source.md", "output": "out.docx"})
    command = processing_command("document.convert", arguments, source, output)
    assert "--sandbox" in command
    assert "--from=gfm-raw_html" in command
    assert "--to=docx" in command
    assert command[-2:] == ["--", str(source)]


@pytest.mark.parametrize("operation", ["media.inspect", "media.thumbnail", "media.extract_audio",
                                       "media.transcode"])
def test_media_has_no_network_protocols_and_explicit_demuxer(operation: str) -> None:
    arguments: dict[str, Any] = {"input": "source.mp4"}
    suffixes = {"media.thumbnail": ".png", "media.extract_audio": ".wav", "media.transcode": ".mp4"}
    if operation != "media.inspect":
        arguments["output"] = "result" + suffixes[operation]
    checked = validate_arguments(operation, arguments)
    command = processing_command(operation, checked, Path("/workspace/source.mp4"),
                                 Path("/workspace/output" + suffixes.get(operation, "")))
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert command[command.index("-f") + 1] == "mov"
    if operation != "media.inspect":
        assert "-nostdin" in command
        assert "-n" in command


def test_write_failure_is_uncertain_redacted_and_not_retried() -> None:
    manager, _, tool, context, transport = setup("document.convert")
    manager.fail = True
    with pytest.raises(ToolExecutionError, match="Leased processing command failed") as error:
        transport(tool, {"input": "report.md", "output": "report.docx"}, context)
    assert error.value.unknown
    assert "sensitive" not in str(error.value)
    assert len(manager.calls) == 1


def test_write_timeout_marks_unknown() -> None:
    manager, _, tool, context, transport = setup("document.convert")
    manager.result = ExecutionResult(exit_code=124)
    with pytest.raises(ToolExecutionError, match="timed out") as error:
        transport(tool, {"input": "report.md", "output": "report.docx"}, context)
    assert error.value.unknown
