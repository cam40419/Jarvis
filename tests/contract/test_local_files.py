import io
import json
import stat
import zipfile
from uuid import uuid4

import pytest

from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.models import Channel
from tests.contract.test_calendar_immediate import pending_calendar
from tests.contract.test_connected import connected_setup


def local_setup(store, tmp_path):
    connected, actor, _ = connected_setup(store)
    host = tmp_path / "host"
    host.mkdir()
    connected.settings = connected.settings.model_copy(
        update={
            "local_files_enabled": True,
            "local_files_dir": tmp_path / "local",
            "local_file_roots": {"downloads": host},
            "local_files_actor_id": actor.actor_id,
        }
    )
    return connected, actor, connected.local_files, host


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def test_local_read_edit_move_zip_extract_and_retry(store, tmp_path):
    _, actor, files, host = local_setup(store, tmp_path)

    def run(name, values, key=None):
        return files.run(actor, name, values, key or str(uuid4()), lambda: actor)

    (host / "bundle.zip").write_bytes(
        zip_bytes([("Stdout/notes.md", "Original text"), ("Stdout/assets/image.bin", b"\x00\x01")])
    )
    listing = run("local_files_list", {"root": "downloads"})
    assert listing["files"][0]["name"] == "bundle.zip"
    assert run("local_zip_inspect", {"root": "downloads", "path": "bundle.zip"})["total"] == 2
    args = {
        "root": "downloads",
        "path": "bundle.zip",
        "destination_root": "workspace",
        "destination_path": "Stdout-import",
    }
    extracted = run("local_zip_extract", args, "extract-once")
    assert extracted == run("local_zip_extract", args, "extract-once")
    assert extracted["entries"] == 2
    path = {"root": "workspace", "path": "Stdout-import/Stdout/notes.md"}
    read = run("local_file_read", path)
    assert read["text"] == "Original text"
    edit = {**path, "revision": read["revision"], "old_text": "Original", "new_text": "Updated"}
    saved = run("local_file_edit", edit, "edit-once")
    assert saved == run("local_file_edit", edit, "edit-once")
    assert run("local_file_read", path)["text"] == "Updated text"
    with pytest.raises(ValidationError, match="changed"):
        run("local_file_edit", edit)
    moved = run(
        "local_file_move",
        {
            **path,
            "revision": saved["revision"],
            "destination_root": "workspace",
            "destination_path": "notes.md",
        },
    )
    assert moved["status"] == "succeeded"
    assert (
        run("local_files_search", {"root": "workspace", "query": "notes"})["files"][0]["path"]
        == "notes.md"
    )
    run(
        "local_zip_create",
        {
            "root": "workspace",
            "path": "notes.md",
            "destination_root": "downloads",
            "destination_path": "result.zip",
        },
    )
    with zipfile.ZipFile(host / "result.zip") as archive:
        assert archive.read("notes.md") == b"Updated text"
    backup = files.workspace(actor) / ".internal" / "versions" / read["revision"]
    assert backup.read_bytes() == b"Original text"


@pytest.mark.parametrize(
    "bad", ["../escape.txt", "C:/escape.txt", "/absolute", "a:stream", "CON.txt", "nested/.env"]
)
def test_zip_rejects_unsafe_entries_before_publication(store, tmp_path, bad):
    _, actor, files, host = local_setup(store, tmp_path)
    (host / "bad.zip").write_bytes(zip_bytes([("good.txt", "Good"), (bad, "Must not extract")]))
    with pytest.raises((AuthorizationError, ValidationError)):
        files.run(
            actor,
            "local_zip_extract",
            {
                "root": "downloads",
                "path": "bad.zip",
                "destination_root": "workspace",
                "destination_path": "extracted",
            },
            "bad-extract",
            lambda: actor,
        )
    assert not (files.workspace(actor) / "extracted").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_zip_links_collisions_and_bombs_rejected(store, tmp_path):
    _, actor, files, host = local_setup(store, tmp_path)
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archives = [
        zip_bytes([(link, "../outside")]),
        zip_bytes([("A.txt", "a"), ("a.txt", "b")]),
        zip_bytes([("folder", "file"), ("folder/child", "child")]),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.txt", b"0" * (2 * 1024 * 1024))
    archives.append(buffer.getvalue())
    for content in archives:
        (host / "bad.zip").write_bytes(content)
        with pytest.raises(ValidationError):
            files.run(
                actor,
                "local_zip_inspect",
                {"root": "downloads", "path": "bad.zip"},
                "inspect",
                lambda: actor,
            )


def test_local_account_boundary_and_no_overwrite(store, tmp_path):
    _, actor, files, host = local_setup(store, tmp_path)
    (host / "keep.txt").write_text("Keep")
    with pytest.raises(ValidationError, match="exists"):
        files.publish(actor, "downloads", "keep.txt", b"Overwrite")
    with pytest.raises(AuthorizationError):
        files.path(actor, "downloads", ".env")
    with pytest.raises(ValidationError):
        files.path(actor, "downloads", "../outside")
    other = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(AuthorizationError):
        files.path(other, "downloads", "keep.txt")
    assert (host / "keep.txt").read_text() == "Keep"


def test_voice_local_tools_and_cancelled_extraction(store, tmp_path):
    connected, actor, files, host = local_setup(store, tmp_path)
    actor = actor.model_copy(update={"channel": Channel.VOICE})
    (host / "source.zip").write_bytes(zip_bytes([("notes.txt", "From voice")]))
    attempt = pending_calendar(connected, actor)
    attempt = attempt.model_copy(
        update={
            "run": attempt.run.model_copy(
                update={
                    "capability_manifest": (
                        "local_files_roots",
                        "local_zip_extract",
                        "local_file_read",
                    ),
                }
            )
        }
    )
    store.save_attempt(attempt)
    execute = connected.executor(actor, attempt.run.id, [], lambda: actor)
    args = {
        "root": "downloads",
        "path": "source.zip",
        "destination_root": "workspace",
        "destination_path": "voice-import",
    }
    result = json.loads(execute("local_zip_extract", json.dumps(args)))
    assert result["status"] == "succeeded"
    assert (
        json.loads(
            execute(
                "local_file_read",
                json.dumps({"root": "workspace", "path": "voice-import/notes.txt"}),
            )
        )["text"]
        == "From voice"
    )
    store.save_attempt(attempt.model_copy(update={"status": "failed"}))
    args["destination_path"] = "cancelled"
    assert "error" in json.loads(execute("local_zip_extract", json.dumps(args)))
    assert not (files.workspace(actor) / "cancelled").exists()


def test_extract_cancellation_leaves_no_partial_destination(store, tmp_path):
    _, actor, files, host = local_setup(store, tmp_path)
    (host / "source.zip").write_bytes(zip_bytes([("one.txt", "one")]))
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls >= 5:
            raise AuthorizationError("Cancelled")
        return actor

    with pytest.raises(AuthorizationError):
        files.run(
            actor,
            "local_zip_extract",
            {
                "root": "downloads",
                "path": "source.zip",
                "destination_root": "workspace",
                "destination_path": "cancelled",
            },
            "cancelled",
            check,
        )
    assert not (files.workspace(actor) / "cancelled").exists()
    assert not list(files.workspace(actor).glob(".simon-unzip-*"))


def test_drive_zip_import_and_export_use_project_boundary(store, tmp_path):
    from simon.adapters.google import DRIVE_WRITE_SCOPE
    from simon.domain.project_files import ProjectCreate, ProjectFileCreate
    from tests.contract.test_google_read_permissions import grant
    from tests.contract.test_project_files import FakeDrive

    connected, actor, files, _ = local_setup(store, tmp_path)
    grant(connected, actor, (DRIVE_WRITE_SCOPE,))
    connected.projects.api = FakeDrive()
    project = connected.projects.create(
        actor, ProjectCreate(name="Stdout", description="Files", idempotency_key="drive-project")
    )
    source = connected.projects.create_file(
        actor,
        ProjectFileCreate(project_id=project["id"], name="bundle.zip"),
        "source-zip",
        lambda: actor,
        raw=zip_bytes([("notes.md", "Imported from Drive")]),
        media_type="application/zip",
    )
    connected.projects.api.download = lambda token, identifier: connected.projects.api.contents[
        identifier
    ]
    args = {
        "root": "workspace",
        "path": "bundle.zip",
        "project_id": project["id"],
        "file_id": source["file_id"],
    }
    imported = files.run(actor, "local_file_import_drive", args, "import-once", lambda: actor)
    assert imported == files.run(
        actor, "local_file_import_drive", args, "import-once", lambda: actor
    )
    files.run(
        actor,
        "local_zip_extract",
        {
            "root": "workspace",
            "path": "bundle.zip",
            "destination_root": "workspace",
            "destination_path": "extracted",
        },
        "extract",
        lambda: actor,
    )
    args = {"root": "workspace", "path": "extracted/notes.md", "project_id": project["id"]}
    exported = files.run(actor, "local_file_export_drive", args, "export-once", lambda: actor)
    assert exported["status"] == "succeeded"
    assert exported == files.run(
        actor, "local_file_export_drive", args, "export-once", lambda: actor
    )
    assert connected.projects.api.contents[exported["file_id"]] == b"Imported from Drive"
