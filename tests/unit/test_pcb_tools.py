from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters import _pcb_runner as runner
from simon.adapters.pcb_tools import (
    PCB_CAPABILITIES,
    PCBToolTransport,
    pcb_configuration_reason,
    pcb_tool_definitions,
)
from simon.domain.errors import AuthorizationError
from simon.domain.execution import ExecutionResult
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError
from tests.unit.test_processing_tools import setup as processing_setup


def setup(operation="pcb.erc"):
    manager, lease, _, context, _ = processing_setup()
    definition = next(tool for tool in pcb_tool_definitions(enabled=True) if tool.id == operation)
    lease = lease.model_copy(
        update={
            "definition": lease.definition.model_copy(
                update={
                    "capabilities": PCB_CAPABILITIES,
                }
            )
        }
    )
    context = context.model_copy(
        update={
            "allowed_tool_ids": frozenset({operation}),
            "scopes": frozenset({"jobs:write"}),
            "authorized_action": "write",
            "environment_capabilities": PCB_CAPABILITIES,
        }
    )
    handler = PCBToolTransport(manager, lease, actor_id=context.actor_id, run_id=context.run_id)
    return manager, lease, definition, context, handler


def test_definitions_are_opt_in_and_require_explicit_workspace_write():
    tools = pcb_tool_definitions()
    assert {tool.id for tool in tools} == set(runner.OPERATIONS)
    assert all(not tool.enabled and not tool.configured for tool in tools)
    assert all(tool.side_effect and tool.required_scopes == {"jobs:write"} for tool in tools)
    assert all(pcb_configuration_reason(tool) is None for tool in tools)


@pytest.mark.parametrize(
    "updates",
    [
        {"required_scopes": frozenset()},
        {"environment_capabilities": frozenset({"python"})},
        {"side_effect": False},
        {"action_policy": "read"},
        {"id": "pcb.unknown"},
    ],
)
def test_configuration_preflight_and_execution_reject_weakened_declarations(updates):
    manager, _, definition, context, handler = setup()
    malformed = definition.model_copy(update=updates)
    assert pcb_configuration_reason(malformed)
    context = context.model_copy(update={"allowed_tool_ids": frozenset({malformed.id})})
    with pytest.raises(ToolCatalogError):
        handler(malformed, {"input": "a.kicad_sch", "output": "a.json"}, context)
    assert not manager.calls


@pytest.mark.parametrize("operation", runner.OPERATIONS)
def test_commands_use_owned_offline_lease_and_fixed_cli(operation):
    manager, lease, definition, context, handler = setup(operation)
    source, target = runner.OPERATIONS[operation]
    handler(definition, {"input": "design" + source, "output": "review" + target}, context)
    identifier, command, attempt, fencing = manager.calls[-1]
    assert (identifier, attempt, fencing) == (
        lease.id,
        lease.plan.request.attempt_id,
        lease.fencing_token,
    )
    assert command.argv[:3] == ("/usr/local/bin/python3", "-I", "-c")
    request = json.loads(command.argv[-1])
    assert request["operation"] == operation and request["timeout_seconds"] == 58
    commands = runner.pcb_commands(
        operation, Path("/workspace/design" + source), Path("/workspace/review" + target)
    )
    assert all(command[0] == "/usr/bin/kicad-cli" for command in commands)
    if operation in {"pcb.erc", "pcb.drc"}:
        assert "--exit-code-violations" in commands[0] and "--severity-all" in commands[0]
    if operation == "pcb.drc":
        assert "--schematic-parity" in commands[0]


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_id": uuid4()},
        {"run_id": uuid4()},
        {"workspace_id": uuid4()},
        {"agent_id": "other"},
        {"scopes": frozenset()},
        {"allowed_tool_ids": frozenset()},
        {"authorized_action": "read"},
    ],
)
def test_transport_rejects_missing_identity_or_action_grants(changes):
    manager, _, definition, context, handler = setup()
    with pytest.raises(AuthorizationError):
        handler(
            definition,
            {"input": "a.kicad_sch", "output": "a.json"},
            context.model_copy(update=changes),
        )
    assert not manager.calls


def test_transport_rejects_network_or_missing_capability():
    manager, _, definition, context, handler = setup()
    networked = handler.lease.definition.model_copy(update={"network": "bridge"})
    handler.lease = handler.lease.model_copy(update={"definition": networked})
    with pytest.raises(ToolCatalogError, match="offline"):
        handler(definition, {"input": "a.kicad_sch", "output": "a.json"}, context)
    assert not manager.calls


@pytest.mark.parametrize(
    "arguments",
    [
        {"input": "../a.kicad_sch", "output": "a.json"},
        {"input": "a.kicad_sch", "output": "/tmp/a.json"},
        {"input": "--option.kicad_sch", "output": "a.json"},
        {"input": "C:/a.kicad_sch", "output": "a.json"},
        {"input": ".git/a.kicad_sch", "output": "a.json"},
        {"input": "a.kicad_pcb", "output": "a.json"},
        {"input": "a.kicad_sch", "output": "a.zip"},
        {"input": "a.kicad_sch", "output": "a.json", "extra": "--exec"},
    ],
)
def test_argument_contract_rejects_paths_extensions_and_extra_options(arguments):
    with pytest.raises(ValueError):
        runner.validate_arguments("pcb.erc", arguments)


def test_checks_preserve_violation_exit_code_in_tool_result():
    manager, _, definition, context, handler = setup()
    manager.result = ExecutionResult(exit_code=5, stdout="violations found")
    result = handler(definition, {"input": "a.kicad_sch", "output": "a.json"}, context)
    assert result["exit_code"] == 5
    manager.result = ExecutionResult(exit_code=124)
    with pytest.raises(ToolExecutionError, match="timed out"):
        handler(definition, {"input": "a.kicad_sch", "output": "a.json"}, context)


def test_violation_report_is_published_without_overwrite(tmp_path, monkeypatch):
    (tmp_path / "design.kicad_sch").write_text("synthetic")
    monkeypatch.setattr(runner.sys, "platform", "test")
    calls = []

    def execute(command, **options):
        calls.append((command, options))
        Path(command[command.index("--output") + 1]).write_text('{"violations":[{}]}')
        return SimpleNamespace(returncode=5)

    monkeypatch.setattr(runner.subprocess, "run", execute)
    request = {
        "operation": "pcb.erc",
        "arguments": {
            "input": "design.kicad_sch",
            "output": "review.json",
        },
        "timeout_seconds": 10,
    }
    assert runner.run(request, tmp_path) == 5
    assert json.loads((tmp_path / "review.json").read_text()) == {"violations": [{}]}
    assert calls[0][1]["env"]["HOME"] == "/tmp"
    assert "API_KEY" not in calls[0][1]["env"]
    with pytest.raises(ValueError, match="exists"):
        runner.run(request, tmp_path)
    assert len(calls) == 1


def test_timeout_does_not_publish_partial_output(tmp_path, monkeypatch):
    (tmp_path / "design.kicad_sch").write_text("synthetic")
    monkeypatch.setattr(runner.sys, "platform", "test")

    def timeout(command, **options):
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    assert (
        runner.run(
            {
                "operation": "pcb.erc",
                "arguments": {
                    "input": "design.kicad_sch",
                    "output": "review.json",
                },
                "timeout_seconds": 10,
            },
            tmp_path,
        )
        == 124
    )
    assert not (tmp_path / "review.json").exists()


def test_drc_requires_matching_schematic(tmp_path):
    (tmp_path / "design.kicad_pcb").write_text("synthetic")
    with pytest.raises(ValueError, match="regular file"):
        runner.run(
            {
                "operation": "pcb.drc",
                "arguments": {
                    "input": "design.kicad_pcb",
                    "output": "review.json",
                },
                "timeout_seconds": 10,
            },
            tmp_path,
        )
