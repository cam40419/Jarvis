from uuid import uuid4

import pytest
from pydantic import ValidationError as SchemaError

from simon.adapters.memory import InMemoryStore
from simon.adapters.workspace_files import WorkspaceFileTransport, workspace_file_definition
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.tool_catalog import ToolExecutionContext
from tests.contract.test_local_files import local_setup
from tests.unit.test_git_tools import setup


def test_workspace_import_preserves_source_and_rejects_other_roots(tmp_path, monkeypatch):
    _, actor, files, _ = local_setup(InMemoryStore(), tmp_path)
    source = files.workspace(actor)
    source.mkdir(parents=True, exist_ok=True)
    (source / "source.csv").write_bytes(b"name,value\nresult,42\n")
    _, lease, _, _, _ = setup()
    workspace = tmp_path / "task"
    workspace.mkdir()
    lease = lease.model_copy(update={
        "plan": lease.plan.model_copy(update={
            "workspace_path": workspace,
            "request": lease.plan.request.model_copy(update={"workspace_id": actor.household_id}),
        }),
        "definition": lease.definition.model_copy(update={
            "capabilities": frozenset({"workspace.write"}),
        }),
    })
    definition = workspace_file_definition()
    context = ToolExecutionContext(
        actor_id=actor.actor_id, household_id=actor.household_id, run_id=uuid4(),
        agent_id=lease.plan.request.agent_id, allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes, environment_capabilities=lease.definition.capabilities,
        authorized_action="write",
    )
    handler = WorkspaceFileTransport(files, lease, actor=actor, run_id=context.run_id,
                                     revalidate=lambda: actor)
    result = handler(definition, {"root": "workspace", "path": "source.csv"}, context)
    assert (workspace / result["path"]).read_bytes() == (source / "source.csv").read_bytes()
    (workspace / result["path"]).write_bytes(b"Edited by worker")
    assert (source / "source.csv").read_bytes() == b"name,value\nresult,42\n"
    with pytest.raises(ValidationError, match="already"):
        handler(definition, {"root": "workspace", "path": "source.csv"}, context)
    with pytest.raises(SchemaError):
        handler(definition, {"root": "downloads", "path": "source.csv"}, context)
    with pytest.raises(ValidationError, match="changed"):
        handler(definition, {"root": "workspace", "path": "source.csv",
                             "expected_sha256": "0" * 64}, context)
    with pytest.raises(AuthorizationError):
        handler(definition, {"root": "workspace", "path": "source.csv"},
                context.model_copy(update={"actor_id": uuid4()}))

    other_workspace = uuid4()
    mismatched = WorkspaceFileTransport(
        files, lease.model_copy(update={"plan": lease.plan.model_copy(update={
            "request": lease.plan.request.model_copy(update={"workspace_id": other_workspace}),
        })}), actor=actor, run_id=context.run_id, revalidate=lambda: actor,
    )
    with pytest.raises(AuthorizationError, match="grant"):
        mismatched(definition, {"root": "workspace", "path": "source.csv"},
                   context.model_copy(update={"household_id": other_workspace}))

    original_blob = files.blob

    def move_root(path):
        content = original_blob(path)
        files.connected.settings = files.connected.settings.model_copy(update={
            "local_files_dir": tmp_path / "relocated-files",
        })
        return content

    monkeypatch.setattr(files, "blob", move_root)
    new_context = context.model_copy(update={"invocation_id": uuid4()})
    with pytest.raises(AuthorizationError, match="source changed"):
        handler(definition, {"root": "workspace", "path": "source.csv"}, new_context)
    assert len(list(workspace.iterdir())) == 1
