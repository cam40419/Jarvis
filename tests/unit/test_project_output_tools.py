"""Actual tool execution and artifact bytes with synthetic model responses only."""

import json
import subprocess
import sys
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.project_output_tools import (
    ProjectOutputToolTransport,
    project_output_definitions,
    project_output_transport_factory,
)
from simon.adapters.workspace_files import (
    WorkspaceFileTransport,
    workspace_artifact_definition,
)
from simon.agent_setup import starter_manifest
from simon.config import Settings
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.execution import EnvironmentDefinition, ExecutionResult
from simon.domain.model_routing import ModelEndpoint
from simon.domain.project_outputs import PromoteProjectOutput
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_outputs import ProjectOutputService
from tests.contract.test_project_outputs import create_output, output_setup
from tests.unit.test_agent_dispatcher import ControlledModel, FakeBackend
from tests.unit.test_git_tools import setup as git_setup


def test_tools_are_run_scoped_bounded_and_reject_contract_downgrades(tmp_path):
    h = output_setup(InMemoryStore(), tmp_path)
    run = create_output(h)
    foreign = create_output(h, 2, project=1)
    definitions = {tool.id: tool for tool in project_output_definitions()}
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id, household_id=h.actor.household_id, run_id=run.id,
        agent_id="writer", allowed_tool_ids=frozenset(definitions), scopes=h.actor.scopes,
        authorized_action="write",
    )
    handler = ProjectOutputToolTransport(h.service, actor=h.actor, run_id=run.id,
                                         revalidate=lambda: h.actor)
    reference = {"run_id": str(run.id), "artifact_id": str(run.tasks[0].artifacts[0].id)}
    read = definitions["project.output_read"]
    response = handler(read, {**reference, "offset": 1, "limit": 3}, context)
    assert response["text"] == "esu" and response["next_offset"] == 4
    assert response["untrusted_source"]
    with pytest.raises(NotFoundError):
        handler(read, {"run_id": str(foreign.id),
                       "artifact_id": str(foreign.tasks[0].artifacts[0].id)}, context)
    with pytest.raises(AuthorizationError):
        handler(read, reference, context.model_copy(update={"run_id": uuid4()}))
    with pytest.raises(AuthorizationError):
        handler(read, reference, context.model_copy(update={"agent_id": "other"}))
    with pytest.raises(ToolCatalogError):
        handler(read.model_copy(update={"required_scopes": frozenset()}), reference, context)
    with pytest.raises(AuthorizationError):
        handler(definitions["project.output_save"], reference,
                context.model_copy(update={"authorized_action": "read"}))
    revoked = ProjectOutputToolTransport(
        h.service, actor=h.actor, run_id=run.id, revalidate=lambda: h.actor.model_copy(update={
            "scopes": h.actor.scopes - {"memories:read"},
        }),
    )
    with pytest.raises(AuthorizationError):
        revoked(read, reference, context)


def test_artifact_import_isolated_filename_integrity_and_revocation(tmp_path):
    h = output_setup(InMemoryStore(), tmp_path)
    source = create_output(h)
    current = create_output(h, 2)
    foreign = create_output(h, 3, project=1)
    _, lease, _, _, _ = git_setup()
    workspace = tmp_path / "isolated"
    workspace.mkdir()
    lease = lease.model_copy(update={
        "plan": lease.plan.model_copy(update={
            "workspace_path": workspace,
            "request": lease.plan.request.model_copy(update={
                "workspace_id": h.actor.household_id, "agent_id": "writer",
            }),
        }), "definition": lease.definition.model_copy(update={
            "capabilities": frozenset({"workspace.write"}),
        }),
    })
    tool = workspace_artifact_definition()
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id, household_id=h.actor.household_id, run_id=current.id,
        agent_id="writer", allowed_tool_ids=frozenset({tool.id}), scopes=h.actor.scopes,
        environment_capabilities=lease.definition.capabilities, authorized_action="write",
    )
    handler = WorkspaceFileTransport(h.files, lease, actor=h.actor, run_id=current.id,
                                     revalidate=lambda: h.actor, outputs=h.service)
    reference = {"run_id": str(source.id), "artifact_id": str(source.tasks[0].artifacts[0].id)}
    result = handler(tool, reference, context)
    assert result["path"] == f"input-{context.invocation_id}.txt"
    assert (workspace / result["path"]).read_bytes() == b"Result"
    (workspace / result["path"]).write_bytes(b"Revised downstream")
    assert h.service.artifacts.read(source.tasks[0].artifacts[0]) == b"Result"
    with pytest.raises(ValidationError, match="changed"):
        handler(tool, {**reference, "expected_sha256": "0" * 64}, context)
    with pytest.raises(NotFoundError):
        handler(tool, {"run_id": str(foreign.id),
                       "artifact_id": str(foreign.tasks[0].artifacts[0].id)}, context)
    with pytest.raises(ToolCatalogError):
        handler(tool.model_copy(update={"action_policy": "read", "side_effect": False}),
                reference, context)
    with pytest.raises(ValidationError, match="already"):
        handler(tool, reference, context)
    calls = 0

    def revoke_before_copy():
        nonlocal calls
        calls += 1
        return h.actor if calls == 1 else h.actor.model_copy(update={
            "scopes": h.actor.scopes - {"jobs:write"},
        })

    handler.revalidate = revoke_before_copy
    with pytest.raises(AuthorizationError):
        handler(tool, reference, context.model_copy(update={"invocation_id": uuid4()}))
    assert len(list(workspace.iterdir())) == 1


def test_dispatcher_passes_real_file_to_dependent_and_reuses_it_in_next_run(tmp_path, monkeypatch):
    h = output_setup(InMemoryStore(), tmp_path)
    python = next(tool for tool in starter_manifest(Settings(model_provider="local")).tools
                  if tool.id == "workspace.python_execute")
    tools = (*project_output_definitions(), workspace_artifact_definition(), python)
    profile = AgentProfile(
        id="writer", instructions="Create and check the requested deliverable.",
        tool_ids=tuple(tool.id for tool in tools), tool_scopes=h.actor.scopes,
        max_action="write", environment_ids=("coding",), max_steps=8,
    )
    team = TeamTemplate(id="studio", name="Studio", agent_ids=(profile.id,))
    platform = AgentPlatformService(h.store, PlatformManifest(
        agents=(profile,), teams=(team,), tools=tools,
        environments=(EnvironmentDefinition(
            id="coding", kind="docker", container_image="unused-test-image", enabled=True,
            capabilities=frozenset({"python", "workspace.write", "process.execute"}),
        ),), models=(ModelEndpoint(
            id="synthetic", provider="openai_compatible", model="synthetic", local=True,
            base_url="http://127.0.0.1:11434/v1", capabilities=frozenset({"text", "tools"}),
        ),),
    ), state_dir=tmp_path / "runtime", environ={},
        available_transports=("project_outputs", "workspace_files", "environment"))
    platform.project_visibility_resolver = h.connected.projects.project
    platform.project_team_resolver = lambda *_: team

    class ControlledPythonBackend(FakeBackend):
        """Only test-owned source is executed; production continues to require Docker."""

        def execute(self, lease, command):
            assert command.argv[:2] == ("python3", "-c")
            completed = subprocess.run(
                [sys.executable, *command.argv[1:]], cwd=lease.plan.workspace_path,
                timeout=5, capture_output=True, text=True, check=False,
            )
            return ExecutionResult(exit_code=completed.returncode,
                                   stdout=completed.stdout, stderr=completed.stderr)

    backend = ControlledPythonBackend()
    platform.environments.backends["docker"] = backend
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: h.actor)
    service = ProjectOutputService(runs, h.files)
    stages = {}
    source_artifact = None
    active_run = None

    def tool_call(identifier, arguments):
        return json.dumps({"type": "tool", "tool_id": identifier, "arguments": arguments})

    def respond(request):
        nonlocal source_artifact
        objective = request.prompt.splitlines()[1]
        stage = stages.get(objective, 0)
        stages[objective] = stage + 1
        if objective == "produce":
            if stage == 0:
                return tool_call(python.id, {"args": ["-c", (
                    "from pathlib import Path; Path('source.csv').write_bytes(b'value\\n42\\n')"
                )]})
            assert '"exit_code":0' in request.prompt
            return json.dumps({"type": "final", "output": "Created original CSV.",
                               "artifacts": ["source.csv"]})
        if objective == "revise":
            source_artifact = runs.get(h.actor, active_run.id).tasks[0].artifacts[1]
            assert str(source_artifact.id) in request.prompt
            assert "Controller-provided dependency file references" in request.prompt
            if stage == 0:
                return tool_call("workspace.import_artifact", {
                    "run_id": str(active_run.id), "artifact_id": str(source_artifact.id),
                    "expected_sha256": source_artifact.sha256,
                })
            if stage == 1:
                assert '"source_artifact_id"' in request.prompt
                return tool_call(python.id, {"args": ["-c", (
                    "from pathlib import Path; p=next(Path('.').glob('input-*.csv')); "
                    "s=p.read_text(); assert s=='value\\n42\\n'; "
                    "Path('revised.csv').write_bytes(s.replace('42','43').encode())"
                )]})
            assert '"exit_code":0' in request.prompt
            return json.dumps({"type": "final", "output": "Read original and verified revision.",
                               "artifacts": ["revised.csv"]})
        assert objective == "reuse-older"
        if stage == 0:
            return tool_call("project.outputs", {})
        if stage == 1:
            assert str(source_artifact.id) in request.prompt
            return tool_call("project.output_read", {
                "run_id": str(source_artifact.run_id), "artifact_id": str(source_artifact.id),
            })
        assert '"text":"value\\n42\\n"' in request.prompt
        return json.dumps({"type": "final", "output": "Verified older immutable original."})

    model = ControlledModel(respond)
    monkeypatch.setattr(
        "simon.services.agent_dispatcher.ModelEndpointClient", lambda *_, **__: model,
    )
    dispatcher = AgentDispatcher(runs, transport_factory=project_output_transport_factory(
        lambda *_: {}, service,
    ))
    request = PlanTeamRequest(
        team_id=team.id, project_id=h.projects[0].id, idempotency_key="file-handoff-plan",
        tasks=(AgentTaskSpec(id="produce", agent_id="writer", objective="produce"),
               AgentTaskSpec(id="revise", agent_id="writer", objective="revise",
                             depends_on=("produce",))),
    )
    plan = platform.plan(h.actor, request)
    assert plan.state == "planned", plan
    active_run = runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="file-handoff-run"))
    result = dispatcher.execute(active_run.id)
    assert result.status == "succeeded", result
    original, revised = result.tasks[0].artifacts[1], result.tasks[1].artifacts[1]
    assert service.artifacts.read(original) == b"value\n42\n"
    assert service.artifacts.read(revised) == b"value\n43\n"
    assert len(backend.released) == 2
    saved = service.promote(h.actor, h.projects[0].id, result.id, revised.id,
                            PromoteProjectOutput(idempotency_key="save-handoff-result"),
                            lambda: h.actor)
    copy = saved.project_copy
    assert h.files.path(h.actor, copy.root, copy.path).read_bytes() == b"value\n43\n"
    later_plan = platform.plan(h.actor, request.model_copy(update={
        "idempotency_key": "reuse-previous-plan", "tasks": (AgentTaskSpec(
            id="reuse", agent_id="writer", objective="reuse-older",
            tool_ids=("project.outputs", "project.output_read"),
        ),),
    }))
    later_run = runs.start(h.actor, later_plan.id, StartAgentRun(idempotency_key="reuse-older-run"))
    assert dispatcher.execute(later_run.id).status == "succeeded"
