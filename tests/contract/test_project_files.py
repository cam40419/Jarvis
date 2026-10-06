import hashlib
import json
from uuid import UUID, uuid4

import pytest

from simon.adapters.google import DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE, ConnectedError
from simon.adapters.project_drive import FOLDER, DriveError, ProjectDriveAPI, text_revision
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.models import Channel, utc_now
from simon.domain.project_files import (
    ProjectBind,
    ProjectCreate,
    ProjectFile,
    ProjectFileCreate,
    ProjectFileEdit,
    ProjectFiles,
)
from simon.domain.tasks import CreateAssistantTask, ProjectArtifact
from simon.services.tasks import AssistantTaskService
from tests.contract.test_calendar_immediate import pending_calendar
from tests.contract.test_connected import connected_setup
from tests.contract.test_google_read_permissions import grant


class FakeDrive(ProjectDriveAPI):
    def __init__(self):
        self.items = {}
        self.contents = {}
        self.creates = 0
        self.edits = 0
        self.timeout_after_create = False

    def generate_id(self, token):
        return uuid4().hex

    def metadata(self, token, identifier):
        if identifier not in self.items:
            raise DriveError("Missing", status=404)
        return dict(self.items[identifier])

    def create(self, token, name, parent, content, mime, identifier, operation):
        identifier = identifier or uuid4().hex
        self.creates += 1
        self.items[identifier] = {
            "id": identifier,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "version": "1",
            "capabilities": {"canEdit": True},
            "url": f"https://drive.google.com/file/d/{identifier}/view",
        }
        self.contents[identifier] = content or b""
        if self.timeout_after_create:
            raise DriveError("Connection lost after create", unknown=True)
        return dict(self.items[identifier])

    def list_files(self, token, folder, query="", page=""):
        return {
            "files": [
                dict(item)
                for item in self.items.values()
                if folder in item["parents"] and query in item["name"]
            ],
            "next_page_token": "",
        }

    def read(self, token, metadata):
        text = self.contents[metadata["id"]].decode()
        return {**metadata, "text": text, "revision": text_revision(text)}

    def edit(self, token, current, old_text, new_text, tab_id):
        self.edits += 1
        text = (
            current["text"].replace(old_text, new_text, 1)
            if old_text
            else current["text"] + new_text
        )
        self.contents[current["id"]] = text.encode()
        self.items[current["id"]]["version"] = str(int(current["version"]) + 1)
        return self.metadata(token, current["id"])


def setup_project(store):
    connected, actor, _ = connected_setup(store)
    grant(connected, actor, (DRIVE_WRITE_SCOPE,))
    files = connected.projects
    files.api = FakeDrive()
    project = files.create(
        actor,
        ProjectCreate(
            name="Build", description="Build a test project", idempotency_key="test-project"
        ),
    )
    return connected, actor, files, UUID(project["id"])


def test_folder_creation_live_files_edits_and_replay(store):
    _, actor, files, project = setup_project(store)
    folder = files.ensure(actor, project)
    assert folder.status == "ready" and files.api.creates == 1
    request = ProjectFileCreate(project_id=project, name="notes.md", content="Original")
    first = files.create_file(actor, request, "create-notes", lambda: actor)
    assert first == files.create_file(actor, request, "create-notes", lambda: actor)
    assert first["status"] == "succeeded" and files.api.creates == 2
    assert len(files.files(actor, ProjectFiles(project_id=project))["files"]) == 1
    identifier = first["file_id"]
    original = files.read(actor, ProjectFile(project_id=project, file_id=identifier))
    edit = ProjectFileEdit(
        project_id=project,
        file_id=identifier,
        revision=original["revision"],
        old_text="Original",
        new_text="Updated",
    )
    saved = files.edit(actor, edit, "edit-notes", lambda: actor)
    assert saved["status"] == "succeeded"
    assert files.edit(actor, edit, "edit-notes", lambda: actor) == saved
    assert files.api.edits == 1
    assert (
        files.read(actor, ProjectFile(project_id=project, file_id=identifier))["text"] == "Updated"
    )
    with pytest.raises(ValidationError, match="changed"):
        files.edit(actor, edit, "stale-edit", lambda: actor)
    assert store.project_file_operation(UUID(saved["id"])).before["text"] == "Original"
    assert store.google_accounts() == ((actor.workspace_id, actor.actor_id),)


def test_project_boundaries_account_changes_and_legacy_scopes(store):
    connected, actor, files, project = setup_project(store)
    files.api.create("token", "outside.txt", None, b"private", "text/plain", "outside", "x")
    with pytest.raises(AuthorizationError, match="outside"):
        files.read(actor, ProjectFile(project_id=project, file_id="outside"))
    other = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(Exception, match="Project not found"):
        files.read(other, ProjectFile(project_id=project, file_id="outside"))
    connection = connected.connection(actor)
    store.delete_google_connection(actor.workspace_id, actor.actor_id)
    store.save_google_connection(
        connection.model_copy(update={"id": uuid4(), "email": "changed@example.com"})
    )
    with pytest.raises(ConnectedError, match="not connected"):
        files.ensure(actor, project)
    store.delete_google_connection(actor.workspace_id, actor.actor_id)
    store.save_google_connection(connection)
    grant(connected, actor, (DRIVE_READ_SCOPE,))
    assert "drive_read_file" in connected.available(actor)
    assert "project_file_edit" not in connected.available(actor)
    with pytest.raises(ConnectedError, match="Reconnect"):
        files.ensure(actor, project)


def test_project_read_provenance_preserves_linked_account_after_default_changes(store):
    connected, actor, files, project = setup_project(store)
    linked = connected.connection(actor)
    saved = files.create_file(
        actor,
        ProjectFileCreate(project_id=project, name="evidence.txt", content="Project evidence"),
        "create-evidence",
        lambda: actor,
    )
    tokens = json.loads(connected.decrypt(linked.encrypted_tokens))
    tokens["access_token"] = "other-account-token"
    other = linked.model_copy(
        update={
            "id": uuid4(),
            "email": "other@example.com",
            "encrypted_tokens": connected.encrypt(json.dumps(tokens)),
        }
    )
    store.save_google_connection(other)
    store.set_default_google_connection(actor.workspace_id, actor.actor_id, other.id)
    observed_tokens = []
    original_read = files.api.read

    def record_read(token, metadata):
        observed_tokens.append(token)
        return original_read(token, metadata)

    files.api.read = record_read
    listing = files.files(actor, ProjectFiles(project_id=project))
    result = files.read(actor, ProjectFile(project_id=project, file_id=saved["file_id"]))
    for response in (listing, result):
        assert response["read_tool"] == "native.project_file_read"
        assert response["read_context"] == {"project_id": str(project)}
        assert response["source_account_id"] == str(linked.id)
        assert "access-secret" not in json.dumps(response)
        assert "other-account-token" not in json.dumps(response)
    assert observed_tokens == ["access-secret"]
    assert result["text"] == "Project evidence"
    assert connected.connection(actor).id == other.id


def test_recovery_after_lost_upload_response_uses_same_id(store):
    _, actor, files, project = setup_project(store)
    files.api.timeout_after_create = True
    request = ProjectFileCreate(project_id=project, name="file.txt", content="Keep me")
    result = files.create_file(actor, request, "upload-retry", lambda: actor, retry_create=True)
    assert result["status"] == "unknown"
    files.api.timeout_after_create = False
    recovered = files.create_file(actor, request, "upload-retry", lambda: actor, retry_create=True)
    assert recovered["status"] == "succeeded" and recovered["file_id"] == result["file_id"]
    assert files.api.creates == 2


def test_background_upload_once_preserves_live_edits_and_relinks(store):
    connected, actor, files, project = setup_project(store)
    tasks = AssistantTaskService(store, connected.identity)
    task = tasks.create(
        actor,
        CreateAssistantTask(
            title="Generate",
            instructions="Make a file",
            project_id=project,
            idempotency_key="artifact-task",
        ),
    )
    content = b"original"
    artifact = ProjectArtifact(
        id=uuid4(),
        workspace_id=actor.workspace_id,
        actor_id=actor.actor_id,
        project_id=project,
        task_id=task.id,
        name="notes.md",
        media_type="text/markdown",
        byte_count=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        created_at=utc_now(),
    )
    store.save_project_artifact(artifact, content)
    assert files.sync(actor, project, force=True)["status"] == "ready"
    live = files.files(actor, ProjectFiles(project_id=project))["files"][0]
    assert live["name"] == "notes.md"
    files.api.contents[live["id"]] = b"edited directly in Drive"
    files.sync(actor, project, force=True)
    assert files.api.creates == 2 and files.api.contents[live["id"]] == b"edited directly in Drive"
    files.api.create("t", "Existing", None, None, FOLDER, "existing", "external")
    binding = files.binding(actor, project)
    files.bind(
        actor,
        ProjectBind(project_id=project, folder_id="existing", expected_version=binding.version),
    )
    with pytest.raises(ValidationError, match="changed"):
        files.bind(
            actor,
            ProjectBind(project_id=project, folder_id="existing", expected_version=binding.version),
        )
    files.sync(actor, project, force=True)
    assert len(files.api.list_files("t", "existing")["files"]) == 1
    assert files.api.contents[live["id"]] == b"edited directly in Drive"


def test_legacy_task_summary_stays_local_and_named_file_syncs(store):
    connected, actor, files, project = setup_project(store)
    tasks = AssistantTaskService(store, connected.identity)
    task = tasks.create(
        actor,
        CreateAssistantTask(
            title="Supplier report",
            instructions="Write a report",
            project_id=project,
            idempotency_key="named-report-task",
        ),
    )
    text = "The report is ready.\n\n```file:supplier-comparison.md\n# Supplier comparison\n```"
    artifacts = tasks.save_artifacts(
        actor, store.get_job(task.id), files.project(actor, project), text
    )
    assert {item.name for item in artifacts} == {"result.md", "supplier-comparison.md"}
    assert files.sync(actor, project, force=True)["status"] == "ready"
    remote = files.files(actor, ProjectFiles(project_id=project))["files"]
    assert [item["name"] for item in remote] == ["supplier-comparison.md"]
    assert files.api.contents[remote[0]["id"]] == b"# Supplier comparison"
    summary = next(item for item in artifacts if item.name == "result.md")
    assert store.project_artifact(summary.id)[1] == text.encode()


def test_pending_legacy_upload_reuses_original_request_after_filename_change(store):
    connected, actor, files, project = setup_project(store)
    tasks = AssistantTaskService(store, connected.identity)
    task = tasks.create(
        actor,
        CreateAssistantTask(
            title="Supplier report",
            instructions="Write a report",
            project_id=project,
            idempotency_key="pending-report-task",
        ),
    )
    artifacts = tasks.save_artifacts(
        actor,
        store.get_job(task.id),
        files.project(actor, project),
        "```file:supplier-comparison.md\n# Suppliers\n```",
    )
    artifact = next(item for item in artifacts if item.name != "result.md")
    binding = files.binding(actor, project)
    key = f"artifact:{artifact.id}:{binding.folder_id}:{binding.google_email}"
    files.api.timeout_after_create = True
    pending = files.create_file(
        actor,
        ProjectFileCreate(project_id=project, name=f"{str(task.id)[:8]}-{artifact.name}"),
        key,
        lambda: actor,
        raw=store.project_artifact(artifact.id)[1],
        media_type=artifact.media_type,
        retry_create=True,
    )
    assert pending["status"] == "unknown"
    files.api.timeout_after_create = False
    assert files.sync(actor, project, force=True)["status"] == "ready"
    assert files.api.creates == 2
    assert len(files.files(actor, ProjectFiles(project_id=project))["files"]) == 1


def test_voice_tool_dispatch_and_cancelled_request(store):
    connected, actor, files, project = setup_project(store)
    actor = actor.model_copy(update={"channel": Channel.VOICE})
    attempt = pending_calendar(connected, actor)
    attempt = attempt.model_copy(
        update={
            "run": attempt.run.model_copy(
                update={
                    "capability_manifest": (
                        "project_list",
                        "project_file_create",
                        "project_file_read",
                    ),
                }
            )
        }
    )
    store.save_attempt(attempt)
    execute = connected.executor(actor, attempt.run.id, [], lambda: actor)
    assert json.loads(execute("project_list", "{}"))[0]["id"] == str(project)
    result = json.loads(
        execute(
            "project_file_create",
            json.dumps(
                {
                    "project_id": str(project),
                    "name": "voice.txt",
                    "content": "Created by voice",
                }
            ),
        )
    )
    assert result["status"] == "succeeded"
    assert (
        json.loads(
            execute(
                "project_file_read",
                json.dumps(
                    {
                        "project_id": str(project),
                        "file_id": result["file_id"],
                    }
                ),
            )
        )["text"]
        == "Created by voice"
    )
    store.save_attempt(attempt.model_copy(update={"status": "failed"}))
    assert "error" in json.loads(
        execute(
            "project_file_create",
            json.dumps(
                {
                    "project_id": str(project),
                    "name": "cancelled.txt",
                    "content": "Must not write",
                }
            ),
        )
    )
    assert files.api.creates == 2


def test_automatic_sweep_and_context_update_keep_folder(store):
    connected, actor, files, project = setup_project(store)
    original_folder = files.binding(actor, project).folder_id
    memory = files.project(actor, project)
    updated = memory.model_copy(
        update={"id": uuid4(), "supersedes": project, "content": "Updated project context"}
    )
    connected.memories.retract(actor, project)
    store.insert_memory(updated)
    files.tick()
    assert files.binding(actor, updated.id).folder_id == original_folder
    assert files.api.creates == 1
    assert files.binding(actor, updated.id).last_synced_at


def test_read_results_withheld_after_disconnect(store):
    connected, actor, files, project = setup_project(store)
    receipt = files.create_file(
        actor,
        ProjectFileCreate(project_id=project, name="private.txt", content="private"),
        "private-create",
        lambda: actor,
    )
    original = files.api.read

    def disconnect(token, metadata):
        connected.disconnect(actor)
        return original(token, metadata)

    files.api.read = disconnect
    with pytest.raises(ConnectedError, match="connection changed"):
        files.read(actor, ProjectFile(project_id=project, file_id=receipt["file_id"]))
