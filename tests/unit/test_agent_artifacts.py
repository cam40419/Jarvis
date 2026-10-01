from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.errors import DomainError
from simon.services.artifacts import ArtifactStore


def publish(store: ArtifactStore, text: str = "An answer.") -> Artifact:
    return store.publish_text(
        workspace_id=uuid4(), actor_id=uuid4(), run_id=uuid4(), task_id=uuid4(), text=text
    )


def content_path(root: Path, artifact: Artifact) -> Path:
    return root.joinpath(
        str(artifact.workspace_id),
        str(artifact.actor_id),
        str(artifact.run_id),
        str(artifact.task_id),
        str(artifact.id),
        "content",
    )


def test_constructor_does_not_create_storage(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    ArtifactStore(root)
    assert not root.exists()


def test_publish_source_bundle_preserves_paths_and_hashes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/main.py").write_bytes(b"print('hello')\n")
    (workspace / "image.png").write_bytes(b"synthetic image bytes")
    store = ArtifactStore(tmp_path / "artifacts")
    (artifact,) = store.publish_workspace_files(
        workspace=workspace,
        paths=("src/main.py", "image.png"),
        workspace_id=uuid4(),
        actor_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
    )
    assert artifact.name == "deliverables.zip"
    with zipfile.ZipFile(io.BytesIO(store.read(artifact))) as archive:
        assert archive.read("src/main.py") == b"print('hello')\n"
        manifest = json.loads(archive.read("simon-deliverables.json"))
        assert len(manifest["files"]) == 2
        entries = {item["path"]: item for item in manifest["files"]}
        assert (
            entries["src/main.py"]["sha256"]
            == hashlib.sha256(archive.read("src/main.py")).hexdigest()
        )


def test_bundle_identity_ignores_collection_time_and_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"First file")
    (workspace / "b.txt").write_bytes(b"Second file")
    store = ArtifactStore(tmp_path / "artifacts")
    references = {
        "workspace_id": uuid4(),
        "actor_id": uuid4(),
        "run_id": uuid4(),
        "task_id": uuid4(),
    }
    monkeypatch.setattr(zipfile.time, "localtime", lambda *_: (2020, 1, 1, 0, 0, 0, 2, 1, -1))
    (original,) = store.publish_workspace_files(
        workspace=workspace, paths=("a.txt", "b.txt"), **references
    )
    monkeypatch.setattr(zipfile.time, "localtime", lambda *_: (2026, 1, 1, 0, 0, 0, 3, 1, -1))
    (repeated,) = store.publish_workspace_files(
        workspace=workspace, paths=("b.txt", "a.txt"), **references
    )
    assert repeated == original
    (workspace / "b.txt").write_bytes(b"Revised second file")
    (revised,) = store.publish_workspace_files(
        workspace=workspace, paths=("a.txt", "b.txt"), **references
    )
    assert revised.id != original.id
    with zipfile.ZipFile(io.BytesIO(store.read(original))) as archive:
        assert archive.read("b.txt") == b"Second file"


@pytest.mark.parametrize("path", ["../secret", "/secret", ".env", ".git/config", "missing"])
def test_workspace_publication_rejects_private_or_invalid_paths(tmp_path: Path, path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(DomainError):
        store.publish_workspace_files(
            workspace=workspace,
            paths=(path,),
            workspace_id=uuid4(),
            actor_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
        )
    assert not store.root.exists()


def test_publish_roundtrip_utf8_and_descriptor_has_no_local_path(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = publish(store, "Research: café ☕")
    expected = "Research: café ☕".encode()
    assert store.read(artifact) == expected
    assert artifact.size == len(expected)
    assert artifact.sha256 == hashlib.sha256(expected).hexdigest()
    assert "path" not in artifact.model_dump_json()
    assert artifact.created_at.tzinfo is not None


def test_identical_publication_is_idempotent_and_changed_content_is_new(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    original = publish(store)
    references = {
        "workspace_id": original.workspace_id,
        "actor_id": original.actor_id,
        "run_id": original.run_id,
        "task_id": original.task_id,
    }
    same = store.publish_text(**references, text="An answer.")
    changed = store.publish_text(**references, text="A revised answer.")
    assert same == original
    assert changed.id != original.id
    assert store.read(original) == b"An answer."
    assert store.read(changed) == b"A revised answer."


def test_concurrent_identical_publications_return_one_complete_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    references = {
        "workspace_id": uuid4(),
        "actor_id": uuid4(),
        "run_id": uuid4(),
        "task_id": uuid4(),
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(store.publish_text, **references, text="Concurrent result")
            for _ in range(16)
        ]
        artifacts = [future.result() for future in futures]
    assert all(artifact == artifacts[0] for artifact in artifacts)
    assert store.read(artifacts[0]) == b"Concurrent result"
    assert not list(tmp_path.rglob(".publishing-*"))


@pytest.mark.parametrize("name", ["../outside", "absolute/file", "C:\\secret", "..", "bad\n"])
def test_names_cannot_supply_paths(tmp_path: Path, name: str) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactError, match="metadata is invalid"):
        store.publish_text(
            workspace_id=uuid4(),
            actor_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
            text="text",
            name=name,
        )
    assert not list(tmp_path.iterdir())


def test_storage_limit_counts_bytes_and_applies_to_reads(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, max_bytes=4)
    with pytest.raises(ArtifactError, match="size limit"):
        publish(store, "ééé")
    artifact = publish(store, "éé")
    with pytest.raises(ArtifactError, match="read limit"):
        store.read(artifact, max_bytes=3)
    assert store.read(artifact) == "éé".encode()


def test_tampered_content_is_never_returned_or_overwritten(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = publish(store)
    content_path(tmp_path, artifact).write_bytes(b"Altered!!!")
    with pytest.raises(ArtifactError, match="integrity"):
        store.read(artifact)
    with pytest.raises(ArtifactError, match="integrity"):
        store.publish_text(
            workspace_id=artifact.workspace_id,
            actor_id=artifact.actor_id,
            run_id=artifact.run_id,
            task_id=artifact.task_id,
            text="An answer.",
        )
    assert content_path(tmp_path, artifact).read_bytes() == b"Altered!!!"


def test_tampered_metadata_or_reference_cannot_read_another_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = publish(store)
    with pytest.raises(ArtifactError):
        store.read(artifact.model_copy(update={"actor_id": uuid4()}))
    with pytest.raises(ArtifactError, match="identity"):
        store.read(artifact.model_copy(update={"name": "changed.txt"}))
    metadata = content_path(tmp_path, artifact).with_name("metadata.json")
    metadata.write_text(
        artifact.model_copy(update={"actor_id": uuid4()}).model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(ArtifactError, match="authorized reference"):
        store.read(artifact)


def test_symlinks_cannot_redirect_storage_reads(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = publish(store)
    path = content_path(store.root, artifact)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"An answer.")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create filesystem symlinks")
    with pytest.raises(ArtifactError, match="redirect"):
        store.read(artifact)


def test_symlink_root_is_rejected_before_publication(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "artifacts"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows account cannot create filesystem symlinks")
    with pytest.raises(ArtifactError, match="redirect"):
        ArtifactStore(root)


def test_invalid_storage_limits_rejected(tmp_path: Path) -> None:
    for invalid in (0, -1, True, 50 * 1024 * 1024 + 1):
        with pytest.raises(ValueError):
            ArtifactStore(tmp_path, max_bytes=invalid)


def test_windows_reparse_point_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "junction"
    original_lstat = Path.lstat

    def reparse_lstat(path: Path) -> Any:
        if path == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(ArtifactError, match="redirect"):
        ArtifactStore(root)


def test_partial_publication_is_not_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)
    write_file = store._write_file

    def fail_metadata(path: Path, data: bytes) -> None:
        if path.name == "metadata.json":
            raise OSError("disk unavailable")
        write_file(path, data)

    monkeypatch.setattr(store, "_write_file", fail_metadata)
    with pytest.raises(ArtifactError, match="could not be written"):
        publish(store)
    assert not list(tmp_path.rglob("content"))
    assert not list(tmp_path.rglob("metadata.json"))
    assert not list(tmp_path.rglob(".publishing-*"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_fifo_deliverable_fails_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "output.txt"
    os.mkfifo(fifo)
    code = """
import sys
from pathlib import Path
from simon.services.artifacts import ArtifactStore
from simon.domain.artifacts import ArtifactError
root = Path(sys.argv[1])
try:
    ArtifactStore(root)._read_file(root / "output.txt", 1024)
except ArtifactError:
    pass
else:
    raise AssertionError("FIFO was accepted")
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=5)
