import json
from uuid import uuid4

import pytest

from simon.adapters import _cad_runner as runner
from simon.adapters.cad_tools import CAPABILITIES, CadToolTransport, cad_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import AuthorizationError
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentRequest,
    ExecutionResult,
)
from simon.domain.models import utc_now
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError


@pytest.fixture
def runtime(tmp_path):
    definition = EnvironmentDefinition(
        id="cad",
        kind="docker",
        container_image="simon-cad:local",
        enabled=True,
        capabilities=CAPABILITIES,
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(),
        agent_id="cad",
        task_id=uuid4(),
        attempt_id=uuid4(),
        capabilities=CAPABILITIES,
    )
    lease = EnvironmentLease(
        id=uuid4(),
        plan=EnvironmentPlan(
            environment_id="cad",
            kind="docker",
            request=request,
            workspace_path=tmp_path,
            network="none",
            cpu_limit=2,
            memory_mb=2048,
            gpu_devices=(),
        ),
        definition=definition,
        fencing_token=9,
        status="active",
        created_at=utc_now(),
        heartbeat_at=utc_now(),
    )
    tools = {tool.id: tool for tool in cad_tool_definitions(enabled=True)}
    context = ToolExecutionContext(
        actor_id=uuid4(),
        household_id=request.workspace_id,
        run_id=uuid4(),
        agent_id="cad",
        scopes=frozenset({"jobs:read", "jobs:write"}),
        allowed_tool_ids=frozenset(tools),
        authorized_action="write",
        environment_capabilities=CAPABILITIES,
    )

    class Manager:
        def __init__(self):
            self.calls = []
            self.result = ExecutionResult(exit_code=0, stdout='{"watertight":true}')

        def execute(self, identifier, command, **ownership):
            self.calls.append((identifier, command, ownership))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    manager = Manager()
    transport = CadToolTransport(manager, lease, actor_id=context.actor_id, run_id=context.run_id)
    return manager, lease, tools, context, transport


def test_definitions_are_optional_and_execute_only_in_exact_lease(runtime):
    assert all(not item.enabled and not item.configured for item in cad_tool_definitions())
    manager, lease, tools, context, transport = runtime
    registry = TransportRegistry()
    registry.register("cad", transport)
    output = registry.execute(tools["cad.mesh_inspect"], {"input": "shape.stl"}, context).output
    assert json.loads(output["stdout"])["watertight"]
    identifier, command, ownership = manager.calls[0]
    assert identifier == lease.id
    assert ownership == {"attempt_id": lease.plan.request.attempt_id, "fencing_token": 9}
    assert command.argv[:3] == ("/usr/local/bin/python3", "-I", "-c")
    assert json.loads(command.argv[4])["arguments"] == {"input": "shape.stl", "vase_checks": False}


@pytest.mark.parametrize("field", ["actor_id", "household_id", "run_id", "agent_id"])
def test_cross_assignment_is_rejected_before_execution(runtime, field):
    manager, _, tools, context, transport = runtime
    value = "other" if field == "agent_id" else uuid4()
    with pytest.raises(AuthorizationError):
        transport(
            tools["cad.mesh_inspect"],
            {"input": "shape.stl"},
            context.model_copy(update={field: value}),
        )
    assert not manager.calls


@pytest.mark.parametrize(
    "updates",
    [
        {"scopes": frozenset()},
        {"allowed_tool_ids": frozenset()},
        {"authorized_action": "read"},
    ],
)
def test_exports_require_write_permission(runtime, updates):
    manager, _, tools, context, transport = runtime
    with pytest.raises(AuthorizationError):
        transport(
            tools["cad.openscad_export"],
            {"input": "source.scad", "output": "out.stl"},
            context.model_copy(update=updates),
        )
    assert not manager.calls


@pytest.mark.parametrize(
    "updates",
    [
        {"side_effect": False},
        {"action_policy": "read"},
        {"required_scopes": frozenset()},
        {"environment_capabilities": frozenset()},
        {"enabled": False},
        {"transport": "http"},
    ],
)
def test_weakened_declarations_are_rejected(runtime, updates):
    manager, _, tools, context, transport = runtime
    with pytest.raises(ToolCatalogError):
        transport(
            tools["cad.openscad_export"].model_copy(update=updates),
            {"input": "source.scad", "output": "out.stl"},
            context,
        )
    assert not manager.calls


@pytest.mark.parametrize("updates", [{"network": "bridge"}, {"capabilities": frozenset()}])
def test_requires_offline_equipped_docker(runtime, updates):
    manager, lease, tools, context, _ = runtime
    changed = lease.model_copy(update={"definition": lease.definition.model_copy(update=updates)})
    transport = CadToolTransport(manager, changed, actor_id=context.actor_id, run_id=context.run_id)
    with pytest.raises(ToolCatalogError):
        transport(tools["cad.mesh_inspect"], {"input": "shape.stl"}, context)
    assert not manager.calls


@pytest.mark.parametrize(
    "path",
    [
        "../out.stl",
        "/etc/file.stl",
        "C:/out.stl",
        ".git/out.stl",
        "https://host/out.stl",
        "--out.stl",
        "bad\x00.stl",
    ],
)
def test_paths_cannot_escape_or_inject_options(runtime, path):
    manager, _, tools, context, transport = runtime
    with pytest.raises(ToolCatalogError):
        transport(tools["cad.mesh_inspect"], {"input": path}, context)
    assert not manager.calls


@pytest.mark.parametrize(
    "arguments",
    [
        {"input": "a.stl", "output": "a.png", "samples": 129},
        {"input": "a.stl", "output": "a.png", "resolution": True},
        {"input": "a.stl", "output": "a.png", "save_scene": "yes"},
        {"input": "a.stl", "output": "a.png", "material": "url"},
        {"input": "a.blend", "output": "a.png"},
        {"input": "a.stl", "output": "a.png", "script": "arbitrary.py"},
    ],
)
def test_renderer_is_bounded_and_does_not_accept_user_code(arguments):
    with pytest.raises(ValueError):
        runner.validate_arguments("cad.render_mesh", arguments)


def test_timeout_and_execution_failure_are_unknown_only_for_writes(runtime):
    manager, _, tools, context, transport = runtime
    for failure in (RuntimeError("secret command details"), ExecutionResult(exit_code=124)):
        manager.result = failure
        for tool_id, arguments, unknown in (
            ("cad.mesh_inspect", {"input": "shape.stl"}, False),
            ("cad.openscad_export", {"input": "shape.scad", "output": "out.stl"}, True),
        ):
            with pytest.raises(ToolExecutionError) as caught:
                transport(tools[tool_id], arguments, context)
            assert caught.value.unknown == unknown and "secret" not in str(caught.value)


def test_runner_rejects_output_collision_directories_and_links(tmp_path):
    source = tmp_path / "existing.stl"
    source.write_bytes(b"existing")
    with pytest.raises(ValueError, match="exists"):
        runner.workspace_file(tmp_path, source.name, output=True)
    folder = tmp_path / "folder.stl"
    folder.mkdir()
    with pytest.raises(ValueError, match="regular"):
        runner.workspace_file(tmp_path, folder.name)
    linked = tmp_path / "linked.stl"
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("Symbolic links unavailable")
    with pytest.raises(ValueError, match="links"):
        runner.workspace_file(tmp_path, linked.name)
    assert source.read_bytes() == b"existing"


def test_blender_scripting_is_explicit_and_requires_extra_capability(runtime):
    manager, lease, _, context, _ = runtime
    assert "cad.blender_script" not in {tool.id for tool in cad_tool_definitions()}
    definition = next(
        tool
        for tool in cad_tool_definitions(enabled=True, include_scripting=True)
        if tool.id == "cad.blender_script"
    )
    context = context.model_copy(update={"allowed_tool_ids": frozenset({definition.id})})
    transport = CadToolTransport(manager, lease, actor_id=context.actor_id, run_id=context.run_id)
    with pytest.raises(ToolCatalogError):
        transport(definition, {"input": "scene.py", "output": "scene.blend"}, context)
    assert not manager.calls
    capabilities = CAPABILITIES | {"blender.script"}
    lease = lease.model_copy(
        update={"definition": lease.definition.model_copy(update={"capabilities": capabilities})}
    )
    context = context.model_copy(update={"environment_capabilities": capabilities})
    transport = CadToolTransport(manager, lease, actor_id=context.actor_id, run_id=context.run_id)
    transport(definition, {"input": "scene.py", "output": "scene.blend"}, context)
    payload = json.loads(manager.calls[0][1].argv[-1])
    assert payload["operation"] == "cad.blender_script"
    assert payload["arguments"] == {"input": "scene.py", "output": "scene.blend"}


def test_blender_script_runner_stages_native_output_and_refuses_overwrite(tmp_path, monkeypatch):
    import ast
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setitem(
        sys.modules, "resource", SimpleNamespace(RLIMIT_FSIZE=1, setrlimit=lambda *_: None)
    )
    (tmp_path / "scene.py").write_text("import bpy\nbpy.ops.mesh.primitive_cube_add()")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        source = Path(command[-1]).read_text()
        tree = ast.parse(source)
        save = tree.body[-1].value
        destination = Path(ast.literal_eval(save.keywords[0].value))
        destination.write_bytes(b"BLENDER-native-scene")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    request = {
        "operation": "cad.blender_script",
        "arguments": {"input": "scene.py", "output": "result.blend"},
        "timeout_seconds": 30,
    }
    assert runner.run(request, tmp_path) == 0
    assert (tmp_path / "result.blend").read_bytes() == b"BLENDER-native-scene"
    assert commands[0][0] == "/usr/bin/blender"
    assert "--disable-autoexec" in commands[0]
    with pytest.raises(ValueError, match="Output exists"):
        runner.run(request, tmp_path)
    assert len(commands) == 1
