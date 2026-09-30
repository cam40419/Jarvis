import json
from uuid import uuid4

import pytest

from simon.adapters.project_drive import FOLDER, DriveError
from simon.domain.errors import NotFoundError, ValidationError
from simon.domain.models import Channel
from simon.domain.project_files import (
    DriveBrowse,
    ProjectBind,
    ProjectFiles,
    ProjectTrash,
    ProjectUnlink,
)
from tests.contract.test_calendar_immediate import pending_calendar
from tests.contract.test_project_files import FakeDrive, setup_project


class BrowsableDrive(FakeDrive):
    def __init__(self, original):
        self.__dict__.update(original.__dict__)
        self.trashes = 0
        self.fail_after_trash = False
        for item in self.items.values():
            item["capabilities"]["canTrash"] = True
            if not item["parents"]:
                item["parents"] = ["my-drive"]
        self.items["my-drive"] = dict(
            id="my-drive",
            name="My Drive",
            mimeType=FOLDER,
            parents=[],
            version="1",
            capabilities={"canEdit": False, "canAddChildren": True, "canTrash": False},
        )

    def metadata(self, token, identifier):
        item = super().metadata(token, "my-drive" if identifier == "root" else identifier)
        if item.get("trashed"):
            raise DriveError("In trash", status=404)
        return item

    def list_files(self, token, folder, query="", page="", *, folders_only=False, search_all=False):
        return {
            "files": [
                dict(item)
                for item in self.items.values()
                if item["id"] != "my-drive"
                and not item.get("trashed")
                and (search_all or folder in item["parents"])
                and (not folders_only or item["mimeType"] == FOLDER)
                and query in item["name"]
            ],
            "next_page_token": "",
        }

    def trash(self, token, current):
        self.trashes += 1
        self.items[current["id"]]["trashed"] = True
        if self.fail_after_trash:
            raise DriveError("Lost response", unknown=True)
        return dict(self.items[current["id"]])


def setup_management(store):
    connected, actor, files, project = setup_project(store)
    files.api = BrowsableDrive(files.api)
    return connected, actor, files, project


def test_browse_root_relink_and_unlink_stops_sync(store):
    _, actor, files, project = setup_management(store)
    original = files.binding(actor, project)
    browsed = files.browse(actor, DriveBrowse(), lambda: actor)
    assert browsed["folder"]["id"] == "my-drive"
    assert browsed["files"][0]["id"] == original.folder_id
    found = files.browse(actor, DriveBrowse(query="Simon", search_all=True), lambda: actor)
    assert found["files"][0]["id"] == original.folder_id
    linked = files.bind(
        actor, ProjectBind(project_id=project, folder_id="root", expected_version=original.version)
    )
    assert linked["folder_id"] == "my-drive"
    assert files.files(actor, ProjectFiles(project_id=project))["folder"]["id"] == "my-drive"
    request = ProjectUnlink(project_id=project, expected_version=linked["version"])
    unlinked = files.unlink(actor, request)
    assert unlinked["status"] == "unlinked" and unlinked["folder_id"] is None
    assert files.unlink(actor, request) == unlinked
    files.tick()
    assert files.sync(actor, project, force=True)["status"] == "unlinked"
    assert files.api.creates == 1
    with pytest.raises(ValidationError, match="no linked"):
        files.files(actor, ProjectFiles(project_id=project))
    assert not files.api.items[original.folder_id].get("trashed")
    linked = files.bind(
        actor,
        ProjectBind(project_id=project, folder_id="root", expected_version=unlinked["version"]),
    )
    assert linked["enabled"] and linked["status"] == "ready"


def test_trash_old_folder_preserves_root_and_durable_receipt(store):
    _, actor, files, project = setup_management(store)
    binding = files.binding(actor, project)
    request = ProjectTrash(project_id=project, file_id=binding.folder_id, revision="1")
    with pytest.raises(ValidationError, match="Relink or unlink"):
        files.trash(actor, request, "trash-old", lambda: actor)
    files.bind(
        actor, ProjectBind(project_id=project, folder_id="root", expected_version=binding.version)
    )
    result = files.trash(actor, request, "trash-old", lambda: actor)
    assert result["status"] == "succeeded" and result["result"]["trashed"]
    assert not result["result"]["permanently_deleted"]
    assert files.trash(actor, request, "trash-old", lambda: actor) == result
    assert files.api.trashes == 1
    with pytest.raises(ValidationError, match="My Drive"):
        files.trash(
            actor,
            ProjectTrash(project_id=project, file_id="root", revision="1"),
            "trash-root",
            lambda: actor,
        )
    assert files.api.trashes == 1


def test_trash_revision_unknown_result_and_account_isolation(store):
    _, actor, files, project = setup_management(store)
    binding = files.binding(actor, project)
    files.unlink(actor, ProjectUnlink(project_id=project, expected_version=binding.version))
    request = ProjectTrash(project_id=project, file_id=binding.folder_id, revision="0")
    with pytest.raises(ValidationError, match="changed"):
        files.trash(actor, request, "stale", lambda: actor)
    files.api.fail_after_trash = True
    request = request.model_copy(update={"revision": "1"})
    receipt = files.trash(actor, request, "unknown-trash", lambda: actor)
    assert receipt["status"] == "unknown"
    assert files.trash(actor, request, "unknown-trash", lambda: actor) == receipt
    assert files.api.trashes == 1
    other = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(NotFoundError):
        files.trash(other, request, "other-account", lambda: other)
    assert files.api.trashes == 1


def test_unlink_survives_project_context_updates(store):
    connected, actor, files, project = setup_management(store)
    files.unlink(
        actor,
        ProjectUnlink(project_id=project, expected_version=files.binding(actor, project).version),
    )
    memory = files.project(actor, project)
    updated = memory.model_copy(update={"id": uuid4(), "supersedes": project})
    connected.memories.retract(actor, project)
    store.insert_memory(updated)
    files.tick()
    assert not files.binding(actor, updated.id).enabled
    assert files.api.creates == 1


def test_shared_voice_dispatch_browse_unlink_trash_and_cancellation(store):
    connected, actor, files, project = setup_management(store)
    actor = actor.model_copy(update={"channel": Channel.VOICE})
    attempt = pending_calendar(connected, actor)
    attempt = attempt.model_copy(
        update={
            "run": attempt.run.model_copy(
                update={"capability_manifest": connected.available(actor)}
            )
        }
    )
    store.save_attempt(attempt)
    binding = files.binding(actor, project)
    execute = connected.executor(actor, attempt.run.id, [], lambda: actor)
    assert json.loads(execute("drive_list_folder", "{}"))["folder"]["id"] == "my-drive"
    result = json.loads(
        execute(
            "project_unlink_drive",
            json.dumps({"project_id": str(project), "expected_version": binding.version}),
        )
    )
    assert result["status"] == "unlinked"
    store.save_attempt(attempt.model_copy(update={"status": "failed"}))
    result = json.loads(
        execute(
            "project_drive_trash",
            json.dumps({"project_id": str(project), "file_id": binding.folder_id, "revision": "1"}),
        )
    )
    assert "error" in result and files.api.trashes == 0
