from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from simon.adapters.environment_tools import EnvironmentCommandTransport
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
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[UUID, ExecutionCommand, UUID, int]] = []
        self.fail = fail

    def execute(
        self, lease_id: UUID, command: ExecutionCommand, *, attempt_id: UUID, fencing_token: int
    ) -> ExecutionResult:
        self.calls.append((lease_id, command, attempt_id, fencing_token))
        if self.fail:
            raise ExecutionError("sensitive runner detail")
        return ExecutionResult(exit_code=0, stdout="rendered", stderr="", truncated=False)


def setup() -> tuple[
    RecordingManager, EnvironmentLease, ToolDefinition, ToolExecutionContext,
    EnvironmentCommandTransport,
]:
    definition = EnvironmentDefinition(
        id="design", kind="docker", container_image="design:latest", enabled=True,
        capabilities=frozenset({"render"}),
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(), agent_id="artist", task_id=uuid4(), attempt_id=uuid4(),
        capabilities=frozenset({"render"}),
    )
    lease = EnvironmentLease(
        id=uuid4(), plan=EnvironmentPlan(
            environment_id=definition.id, kind=definition.kind, request=request,
            workspace_path=Path("unused"), network="none", cpu_limit=2, memory_mb=2048,
            gpu_devices=(),
        ), definition=definition, fencing_token=7, status="active", resource_handle="owned",
        created_at=utc_now(), heartbeat_at=utc_now(),
    )
    tool = ToolDefinition(
        id="design.render", description="Render an approved scene", transport="environment",
        configured=True, side_effect=True, action_policy="write",
        required_scopes=frozenset({"design:write"}),
        environment_capabilities=frozenset({"render"}),
        settings={"argv_prefix": ["/usr/bin/renderer", "--background"]},
        input_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"args": {"type": "array", "items": {"type": "string"}}},
        },
    )
    context = ToolExecutionContext(
        actor_id=uuid4(), household_id=request.workspace_id, run_id=uuid4(), agent_id="artist",
        allowed_tool_ids=frozenset({tool.id}), scopes=frozenset({"design:write"}),
        environment_capabilities=frozenset({"render"}), authorized_action="write",
    )
    manager = RecordingManager()
    transport = EnvironmentCommandTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id,
    )
    return manager, lease, tool, context, transport


def test_bound_transport_uses_only_lease_with_fixed_executable() -> None:
    manager, lease, tool, context, transport = setup()
    registry = TransportRegistry()
    registry.register("environment", transport)
    result = registry.execute(tool, {"args": ["scene.blend", "literal; text"]}, context)
    assert result.output == {
        "exit_code": 0, "stdout": "rendered", "stderr": "", "truncated": False,
    }
    assert len(manager.calls) == 1
    lease_id, command, attempt_id, fence = manager.calls[0]
    assert lease_id == lease.id
    assert attempt_id == lease.plan.request.attempt_id
    assert fence == lease.fencing_token
    assert command.argv == (
        "/usr/bin/renderer", "--background", "scene.blend", "literal; text",
    )


@pytest.mark.parametrize("field", ["actor_id", "household_id", "run_id", "agent_id"])
def test_cross_assignment_context_is_rejected(field: str) -> None:
    manager, _, tool, context, transport = setup()
    other: Any = "other-agent" if field == "agent_id" else uuid4()
    with pytest.raises(AuthorizationError, match="worker lease"):
        transport(tool, {"args": []}, context.model_copy(update={field: other}))
    assert manager.calls == []


@pytest.mark.parametrize(
    "update",
    [
        {"scopes": frozenset()}, {"allowed_tool_ids": frozenset()},
        {"authorized_action": "read"},
    ],
)
def test_missing_command_authorization_rejected(update: dict[str, Any]) -> None:
    manager, _, tool, context, transport = setup()
    with pytest.raises(AuthorizationError):
        transport(tool, {}, context.model_copy(update=update))
    assert manager.calls == []


def test_command_tool_must_explicitly_declare_write() -> None:
    manager, _, tool, context, transport = setup()
    with pytest.raises(ToolCatalogError, match="configured write"):
        transport(tool.model_copy(update={"side_effect": False, "action_policy": "read"}),
                  {}, context)
    assert manager.calls == []


def test_environment_capabilities_checked_against_actual_lease() -> None:
    manager, _, tool, context, transport = setup()
    tool = tool.model_copy(update={"environment_capabilities": frozenset({"cad"})})
    context = context.model_copy(update={"environment_capabilities": frozenset({"cad"})})
    with pytest.raises(ToolCatalogError, match="cannot support"):
        transport(tool, {}, context)
    assert manager.calls == []


@pytest.mark.parametrize(
    "arguments", [{"argv": ["arbitrary"]}, {"args": "raw command"}, {"args": [1]},
                  {"args": ["bad\x00arg"]}],
)
def test_model_cannot_replace_executable_or_supply_invalid_args(arguments: dict[str, Any]) -> None:
    manager, _, tool, context, transport = setup()
    with pytest.raises(ToolCatalogError):
        transport(tool, arguments, context)
    assert manager.calls == []


def test_timeout_and_output_capped_by_worker_limits() -> None:
    manager, _, tool, context, transport = setup()
    tool.settings.update({"timeout_seconds": 500, "max_output_bytes": 999999})
    transport(tool, {}, context)
    assert manager.calls[0][1].timeout_seconds == 60
    assert manager.calls[0][1].max_output_bytes == 65536


def test_failure_is_uncertain_redacted_and_not_retried() -> None:
    manager, _, tool, context, transport = setup()
    manager.fail = True
    with pytest.raises(ToolExecutionError, match="Leased environment command failed") as error:
        transport(tool, {}, context)
    assert error.value.unknown
    assert "sensitive" not in str(error.value)
    assert len(manager.calls) == 1


def test_registry_validates_argument_schema_before_command() -> None:
    manager, _, tool, context, transport = setup()
    tool.input_schema["properties"]["args"]["items"] = {"const": "approved.scene"}
    registry = TransportRegistry()
    registry.register("environment", transport)
    with pytest.raises(ToolCatalogError, match="input schema"):
        registry.execute(tool, {"args": ["unapproved.scene"]}, context)
    assert manager.calls == []


def test_released_lease_cannot_be_bound() -> None:
    manager, lease, _, context, _ = setup()
    with pytest.raises(ToolCatalogError, match="active lease"):
        EnvironmentCommandTransport(
            manager, lease.model_copy(update={"status": "released"}),
            actor_id=context.actor_id, run_id=context.run_id,
        )
