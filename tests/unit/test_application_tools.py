import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters.application_tools import ApplicationToolTransport, desktop_tool_definitions
from simon.desktop_bridge import BridgeRequest, execute, snapshot
from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentDefinition, ExecutionResult
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError
from tests.unit.test_cad_tools import runtime as runtime


def machine(runtime):
    manager, lease, _, context, _ = runtime
    capabilities = frozenset({"application.control", "desktop.windows.uia"})
    definition = EnvironmentDefinition(
        id="desktop",
        kind="machine",
        os="windows",
        enabled=True,
        capabilities=capabilities,
        runner_url="http://localhost:9999",
        credential_env="RUNNER_KEY",
        network="bridge",
    )
    lease = lease.model_copy(
        update={
            "definition": definition,
            "plan": lease.plan.model_copy(
                update={
                    "kind": "machine",
                    "environment_id": "desktop",
                    "network": "bridge",
                }
            ),
        }
    )
    tools = {
        item.id: item
        for item in desktop_tool_definitions(python_executable="python.exe", enabled=True)
    }
    context = context.model_copy(
        update={"allowed_tool_ids": frozenset(tools), "environment_capabilities": capabilities}
    )
    return manager, lease, tools, context


def test_application_bridge_uses_fixed_command_and_owned_lease(runtime):
    manager, lease, tools, context = machine(runtime)
    transport = ApplicationToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id
    )
    transport(tools["desktop.inspect"], {}, context)
    _, command, owner = manager.calls[0]
    assert command.argv[:3] == ("python.exe", "-m", "simon.desktop_bridge")
    payload = json.loads(command.argv[-1])
    assert payload["lease_id"] == str(lease.id)
    assert payload["invocation_id"] == str(context.invocation_id)
    assert owner["fencing_token"] == lease.fencing_token


@pytest.mark.parametrize(
    "change",
    [
        {"actor_id": uuid4()},
        {"household_id": uuid4()},
        {"run_id": uuid4()},
        {"allowed_tool_ids": frozenset()},
        {"scopes": frozenset()},
        {"authorized_action": "read"},
    ],
)
def test_application_bridge_rejects_unowned_or_ungranted_calls(runtime, change):
    manager, lease, tools, context = machine(runtime)
    transport = ApplicationToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id
    )
    with pytest.raises(AuthorizationError):
        transport(tools["desktop.inspect"], {}, context.model_copy(update=change))
    assert not manager.calls


def test_unknown_fields_are_rejected_before_dispatch(runtime):
    manager, lease, tools, context = machine(runtime)
    transport = ApplicationToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id
    )
    with pytest.raises(ToolCatalogError):
        transport(tools["desktop.inspect"], {"executable": "ungranted.exe"}, context)
    assert not manager.calls


def test_failed_bridge_is_uncertain_and_never_retried(runtime):
    manager, lease, tools, context = machine(runtime)
    manager.result = ExecutionResult(exit_code=2)
    transport = ApplicationToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id
    )
    with pytest.raises(ToolExecutionError) as error:
        transport(tools["desktop.inspect"], {}, context)
    assert error.value.unknown
    assert len(manager.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"environment_capabilities": frozenset()},
        {"side_effect": False},
        {"action_policy": "read"},
        {"required_scopes": frozenset()},
    ],
)
def test_weakened_application_declaration_is_rejected(runtime, change):
    manager, lease, tools, context = machine(runtime)
    transport = ApplicationToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id
    )
    with pytest.raises(ToolCatalogError):
        transport(tools["desktop.inspect"].model_copy(update=change), {}, context)
    assert not manager.calls


def test_desktop_observation_is_bounded_before_transport_serialization():
    window = Control()
    window.children = [Control(index, "\u2603" * 160) for index in range(2, 202)]
    view, controls = snapshot(window)
    assert len(json.dumps(view)) < 25000
    assert len(view["controls"]) == len(controls) < 200


class Control:
    def __init__(self, identifier=1, name="Document", password=False):
        self.element_info = SimpleNamespace(
            process_id=123,
            runtime_id=(identifier,),
            control_type="Edit",
            element=SimpleNamespace(CurrentIsPassword=password),
        )
        self.handle, self.name, self.calls = 42, name, []
        self.children = []

    def descendants(self, **kwargs):
        return self.children

    def window_text(self):
        return self.name

    def is_enabled(self):
        return True

    def is_visible(self):
        return True

    def invoke(self):
        self.calls.append("invoke")

    def set_edit_text(self, text):
        self.calls.append(text)


def bridge_request(window, **changes):
    return BridgeRequest(
        version=1,
        operation="invoke",
        arguments={
            "control_id": [1],
            "expected_state": snapshot(window)[0]["state"],
        },
        invocation_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
    ).model_copy(update=changes)


def test_desktop_state_and_receipt_prevent_stale_or_repeated_click(tmp_path):
    window = Control()
    request = bridge_request(window)
    execute(request, window, tmp_path)
    with pytest.raises(FileExistsError):
        execute(request, window, tmp_path)
    assert window.calls == ["invoke"]
    window.name = "Changed document"
    with pytest.raises(ValueError, match="changed"):
        execute(request, window, tmp_path)
    assert window.calls == ["invoke"]
    receipt = next(tmp_path.glob("*.jsonl")).read_text()
    assert "dispatched" in receipt and "completed" in receipt


def test_desktop_filters_password_controls_and_sets_literal_text(tmp_path):
    window = Control()
    window.children = [Control(2, "secret password", True)]
    view, _ = snapshot(window)
    assert "secret password" not in json.dumps(view)
    request = bridge_request(
        window,
        operation="set_text",
        arguments={
            "control_id": [1],
            "expected_state": view["state"],
            "text": "^%{ENTER}",
        },
    )
    execute(request, window, tmp_path)
    assert window.calls == ["^%{ENTER}"]
