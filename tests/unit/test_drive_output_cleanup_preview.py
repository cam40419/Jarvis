from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.apply_drive_output_cleanup import apply_entry
from scripts.preview_drive_output_cleanup import Source, verify_copy
from simon.domain.models import ActorContext, Channel
from simon.domain.project_files import ProjectDrive, ProjectFileOperation
from simon.services.canonical import digest


@pytest.fixture
def saved_copy():
    actor = ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "memories:read"}),
    )
    binding = ProjectDrive(
        project_id=uuid4(),
        workspace_id=actor.workspace_id,
        actor_id=actor.actor_id,
        google_email="owner@example.test",
        folder_id="project-folder",
        version=3,
    )
    content = b"Full original document. Do not print this private content."
    source = Source(
        uuid4(),
        binding.project_id,
        uuid4(),
        "research",
        "agent-output",
        "internal_response",
        "answer.txt",
        "12345678-answer.txt",
        None,
        "text/plain",
        len(content),
        hashlib.sha256(content).hexdigest(),
        lambda: content,
    )
    metadata = {
        "id": "remote-file",
        "name": source.expected_remote_name,
        "mimeType": source.media_type,
        "parents": [binding.folder_id],
        "version": "1",
        "modifiedTime": "2026-10-02T05:00:00Z",
        "size": str(source.size),
        "md5Checksum": hashlib.md5(content, usedforsecurity=False).hexdigest(),
        "trashed": False,
        "_etag": '"original-etag"',
    }
    request = {
        "project_id": str(binding.project_id),
        "kind": "create",
        "data": {
            "name": source.expected_remote_name,
            "parent": binding.folder_id,
            "mime": source.media_type,
            "sha256": source.sha256,
        },
    }
    operation = ProjectFileOperation(
        id=source.operation_id(actor, binding),
        project_id=binding.project_id,
        workspace_id=actor.workspace_id,
        actor_id=actor.actor_id,
        google_email=binding.google_email,
        request_digest=digest(request),
        kind="create",
        status="succeeded",
        file_id=metadata["id"],
        result=deepcopy(metadata),
    )
    return source, actor, binding, operation, content, metadata


def verify(saved_copy, **changes):
    source, actor, binding, operation, content, metadata = saved_copy
    return verify_copy(
        changes.get("source", source),
        changes.get("actor", actor),
        changes.get("binding", binding),
        changes.get("operation", operation),
        changes.get("original", content),
        changes.get("before", metadata),
        changes.get("remote", content),
        changes.get("after", metadata),
    )


def test_only_exact_unchanged_receipt_copy_proposes_trash(saved_copy):
    row = verify(saved_copy)
    assert row["action"] == "trash"
    assert row["verified_unedited"]
    assert all(row["checks"].values())
    assert "Do not print" not in str(row)
    assert row["remote"]["_etag"] == '"original-etag"'


@pytest.mark.parametrize("classification", ["deliverable", "promoted_deliverable"])
def test_named_actual_reports_are_renamed_never_trashed(saved_copy, classification):
    source = replace(
        saved_copy[0],
        classification=classification,
        proposed_name="manufacturing-strategy.md",
        copy={"path": "research/manufacturing-strategy.md", "revision": saved_copy[0].sha256},
    )
    row = verify(saved_copy, source=source)
    assert row["action"] == "rename"
    assert row["proposed_name"] == "manufacturing-strategy.md"
    assert row["source"]["name"] == "answer.txt"


def test_edited_or_missing_explicit_copy_never_authorizes_trash(saved_copy):
    row = verify(
        saved_copy, source=replace(saved_copy[0], classification="promoted_copy_unverified")
    )
    assert row["action"] == "skip"
    assert row["reason"] == "classification_requires_review"


@pytest.mark.parametrize(
    "field,value,check",
    [
        ("name", "renamed-by-user.txt", "name_unchanged"),
        ("parents", ["another-folder"], "parent_unchanged"),
        ("modifiedTime", "2026-10-03T00:00:00Z", "modified_time_unchanged"),
        ("mimeType", "application/vnd.google-apps.folder", "not_directory"),
        ("size", "999", "metadata_size"),
        ("md5Checksum", "a" * 32, "metadata_checksum"),
        ("trashed", True, "not_trashed"),
        ("id", "different-file", "remote_identity"),
    ],
)
def test_user_changes_and_directories_are_excluded(saved_copy, field, value, check):
    metadata = {**saved_copy[5], field: value}
    row = verify(saved_copy, before=metadata, after=metadata)
    assert row["action"] == "skip"
    assert check in row["changed_or_unverified"]


def test_change_between_metadata_reads_is_excluded(saved_copy):
    row = verify(saved_copy, after={**saved_copy[5], "version": "2"})
    assert row["action"] == "skip"
    assert not row["checks"]["stable_during_read"]


def test_provider_version_delta_is_recorded_when_bytes_and_metadata_unchanged(saved_copy):
    metadata = {**saved_copy[5], "version": "3", "_etag": None}
    row = verify(saved_copy, before=metadata, after=metadata)
    assert row["action"] == "trash"
    assert row["verified_unedited"]
    assert row["required_failed_checks"] == []
    assert set(row["changed_or_unverified"]) == {"version_unchanged", "conditional_write_available"}
    assert row["receipt"]["original_version"] == "1"
    assert row["remote"]["version"] == "3"


@pytest.mark.parametrize("which", ["original", "remote"])
def test_real_content_hash_required_not_only_remote_metadata(saved_copy, which):
    row = verify(saved_copy, **{which: b"edited content"})
    assert row["action"] == "skip"
    assert not row["verified_unedited"]


@pytest.mark.parametrize(
    "which,value",
    [
        ("actor_id", uuid4()),
        ("workspace_id", uuid4()),
        ("project_id", uuid4()),
        ("google_email", "other@example.test"),
        ("request_digest", "different"),
        ("kind", "rename"),
        ("status", "unknown"),
        ("id", uuid4()),
        ("file_id", "different-file"),
    ],
)
def test_unrelated_or_incomplete_durable_receipt_cannot_authorize_cleanup(saved_copy, which, value):
    row = verify(saved_copy, operation=saved_copy[3].model_copy(update={which: value}))
    assert row["action"] == "skip"
    assert not row["verified_unedited"]


def test_relinked_folder_excludes_old_copy(saved_copy):
    row = verify(saved_copy, binding=saved_copy[2].model_copy(update={"folder_id": "new-folder"}))
    assert row["action"] == "skip"
    assert not row["checks"]["receipt"]


def executor_fixture(saved_copy, *, name=None):
    source, actor, binding, operation, content, metadata = saved_copy
    metadata = {**metadata, "version": "3", "_etag": None}
    if name:
        source = replace(source, classification="deliverable", proposed_name=name)
    row = verify_copy(source, actor, binding, operation, content, metadata, content, metadata)
    calls = []

    def write(current, request, key, revalidate):
        assert revalidate() == actor
        calls.append((request, key))
        return {"id": str(uuid4()), "file_id": metadata["id"], "status": "succeeded"}

    api = SimpleNamespace(
        metadata=lambda *args: dict(metadata),
        download=lambda *args: content,
        list_files=lambda *args, **kwargs: {"files": [], "next_page_token": ""},
    )
    files = SimpleNamespace(
        current_actor=lambda value: value,
        project=lambda *args: None,
        access=lambda *args, **kwargs: ("synthetic-token", binding.google_email),
        api=api,
        rename=write,
        trash=write,
    )
    store = SimpleNamespace(
        project_drive=lambda *args: binding,
        project_file_operation=lambda identifier: operation if identifier == operation.id else None,
        get_job=lambda *args: None,
    )
    container = SimpleNamespace(connected=SimpleNamespace(projects=files), store=store)
    return container, actor, row, source, calls, metadata


@pytest.mark.parametrize("name", [None, "competition-report.md"])
def test_executor_uses_current_revision_and_ordinary_service_receipt(saved_copy, name):
    container, actor, row, source, calls, _ = executor_fixture(saved_copy, name=name)
    result = apply_entry(container, actor, row, source, "1" * 64)
    assert result["status"] == "succeeded"
    assert len(calls) == 1
    assert calls[0][0].revision == "3"
    assert result["permanently_deleted"] is False
    if name:
        assert calls[0][0].name == name
        assert result["action"] == "rename"
    else:
        assert result["action"] == "trash"


def test_executor_new_revision_refuses_before_any_write(saved_copy):
    container, actor, row, source, calls, metadata = executor_fixture(saved_copy)
    metadata["version"] = "4"
    with pytest.raises(ValueError, match="changed after preview"):
        apply_entry(container, actor, row, source, "1" * 64)
    assert not calls


def test_executor_new_named_publication_preserves_response(saved_copy):
    container, actor, row, source, calls, _ = executor_fixture(saved_copy)
    container.store.get_job = lambda identifier: SimpleNamespace(
        input={"copy": {"path": "research/newly-saved-report.md"}}
    )
    with pytest.raises(ValueError, match="publication changed"):
        apply_entry(container, actor, row, source, "1" * 64)
    assert not calls


def test_executor_never_replays_unknown_cleanup_write(saved_copy):
    container, actor, row, source, calls, _ = executor_fixture(saved_copy)
    original = saved_copy[3]
    unknown = original.model_copy(update={"kind": "trash", "status": "unknown"})
    container.store.project_file_operation = lambda identifier: (
        original if identifier == original.id else unknown
    )
    result = apply_entry(container, actor, row, source, "1" * 64)
    assert result["status"] == "unknown"
    assert result["replayed_receipt"] is True
    assert not calls


def test_executor_wont_create_a_duplicate_named_deliverable(saved_copy):
    container, actor, row, source, calls, _ = executor_fixture(saved_copy, name="report.md")
    container.connected.projects.api.list_files = lambda *args, **kwargs: {
        "files": [{"id": "other-report", "name": "report.md"}],
        "next_page_token": "",
    }
    with pytest.raises(ValueError, match="already has"):
        apply_entry(container, actor, row, source, "1" * 64)
    assert not calls
