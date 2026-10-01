import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from simon.domain.errors import AuthorizationError, ValidationError
from simon.services import local_files, safe_files
from simon.services.local_files import LocalFileService


def link_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Symbolic links are unavailable for this test account")


def test_blob_reads_stable_regular_file_and_rejects_oversize_or_missing(tmp_path):
    source = tmp_path / "ordinary.txt"
    source.write_bytes(b"normal file")
    assert LocalFileService.blob(source) == b"normal file"
    with pytest.raises(ValidationError, match="exceeds"):
        LocalFileService.blob(source, 2)
    with pytest.raises(ValidationError, match="unavailable"):
        LocalFileService.blob(tmp_path / "missing.txt")
    with pytest.raises(ValidationError, match="unavailable"):
        LocalFileService.blob(tmp_path)


def test_blob_rejects_preexisting_leaf_and_ancestor_links(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_bytes(b"private")
    leaf = tmp_path / "leaf.txt"
    link_or_skip(leaf, external / "secret.txt")
    ancestor = tmp_path / "ancestor"
    link_or_skip(ancestor, external, directory=True)
    for path in (leaf, ancestor / "secret.txt"):
        with pytest.raises(AuthorizationError):
            LocalFileService.blob(path)


@pytest.mark.parametrize("swap_parent", [False, True], ids=["leaf", "ancestor"])
def test_blob_rejects_link_swap_after_validation_before_any_read(
    tmp_path,
    monkeypatch,
    swap_parent,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "file.txt"
    source.write_bytes(b"authorized")
    external = tmp_path / "external"
    external.mkdir()
    secret = external / source.name
    secret.write_bytes(b"must never read")
    capability_check = tmp_path / "link-check"
    link_or_skip(capability_check, secret)
    capability_check.unlink()
    original = local_files.reject_links

    def replace_after_check(path):
        original(path)
        if swap_parent:
            workspace.rename(tmp_path / "original-workspace")
            workspace.symlink_to(external, target_is_directory=True)
        else:
            source.unlink()
            source.symlink_to(secret)

    def forbidden_read(*args, **kwargs):
        pytest.fail("Redirected content must not reach a readable Python stream")

    monkeypatch.setattr(local_files, "reject_links", replace_after_check)
    monkeypatch.setattr(safe_files.os, "fdopen", forbidden_read)
    with pytest.raises(ValidationError, match="unavailable"):
        LocalFileService.blob(source)


def test_blob_rejects_mutation_during_read(tmp_path, monkeypatch):
    source = tmp_path / "changing.txt"
    source.write_bytes(b"original")
    original = local_files.open_regular_nofollow

    @contextmanager
    def changing(path):
        with original(path) as stream:

            def read(limit):
                content = stream.read(limit)
                with source.open("ab") as output:
                    output.write(b"changed")
                return content

            yield SimpleNamespace(fileno=stream.fileno, read=read)

    monkeypatch.setattr(local_files, "open_regular_nofollow", changing)
    with pytest.raises(ValidationError, match="changed while reading"):
        LocalFileService.blob(source)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO test")
def test_blob_rejects_fifo_without_blocking(tmp_path):
    source = tmp_path / "pipe"
    os.mkfifo(source)
    with pytest.raises(ValidationError, match="unavailable"):
        LocalFileService.blob(source)
