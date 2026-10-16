"""Admitted source bytes retain accountable storage through failed writes and cleanup."""

import base64
import hashlib
from uuid import uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.native_intake import ReconcileSourceUpload, SourceUpload
from simon.domain.native_projects import UpdateNativeProject
from simon.domain.native_storage import StorageCharge, StorageEntitlement
from tests.unit.test_native_intake import configure, setup_intake


def upload_command(h, raw=b"Source evidence.", **changes):
    current = h.store.native_intake(h.workspace, h.first.id)
    return SourceUpload(
        **{
            "source_key": "evidence.txt",
            "filename": "evidence.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(raw).decode(),
            "expected_version": current.version if current else 0,
            "idempotency_key": str(uuid4()),
            **changes,
        }
    )


def held(h):
    return [
        charge
        for charge in h.store.storage_charges(h.workspace, h.first.id)
        if charge.object_kind == "source" and charge.state == "reserved"
    ]


def reconcile(h, charge, *, command=None):
    current = h.store.native_intake(h.workspace, h.first.id)
    return h.intake.reconcile_upload(
        h.owner_actor,
        h.first.id,
        charge.object_id,
        command
        or ReconcileSourceUpload(
            expected_version=current.version if current else 0,
            reason="Discard the interrupted source upload.",
            idempotency_key=str(uuid4()),
        ),
    )


def quota(h, limit):
    current = h.store.storage_entitlement(h.workspace)
    h.store.save_storage_entitlement(
        StorageEntitlement(
            workspace_id=h.workspace,
            version=current.version + 1 if current else 1,
            plan_key="synthetic",
            limit_bytes=limit,
            issued_by=h.owner,
            reason="Synthetic storage test grant.",
        ),
        current.version if current else 0,
    )


def fail_after_blob(h, monkeypatch):
    original = h.store.insert_native_intake_source

    def fail(_source):
        raise RuntimeError("Synthetic metadata persistence failure.")

    monkeypatch.setattr(h.store, "insert_native_intake_source", fail)
    return original


def test_failed_metadata_keeps_durable_hold_and_same_command_finishes_once(tmp_path, monkeypatch):
    h = setup_intake(tmp_path, free=True)
    raw = "Retained résumé evidence.\n".encode()
    command = upload_command(h, raw)
    original = fail_after_blob(h, monkeypatch)
    with pytest.raises(RuntimeError, match="metadata persistence"):
        h.intake.upload(h.owner_actor, h.first.id, command)
    charge = held(h)[0]
    assert charge.bytes == len(raw) + len(raw.decode().strip().encode())
    assert h.store.native_intake_sources(h.workspace, h.first.id) == ()
    assert h.intake.source_bytes.read(h.workspace, h.first.id, charge.sha256, len(raw)) == raw
    pending = h.intake.view(h.owner_actor, h.first.id)["pending_uploads"]
    assert pending[0]["source_id"] == str(charge.object_id) and pending[0]["bytes"] == charge.bytes
    assert "request_fingerprint" not in pending[0] and "sha256" not in pending[0]
    monkeypatch.setattr(h.store, "insert_native_intake_source", original)
    source = h.intake.upload(h.owner_actor, h.first.id, command)
    assert source.id == charge.object_id and source.created_at == charge.created_at
    assert h.store.storage_charge(h.workspace, "source", source.id).state == "retained"
    quota(h, 0)
    replay = h.intake.upload(h.owner_actor, h.first.id, command)
    assert replay == source
    assert len(h.store.storage_charges(h.workspace)) == 1
    assert not held(h) and not h.calls


def test_changed_retry_rejected_and_stale_claim_requires_explicit_reconciliation(
    tmp_path, monkeypatch
):
    h = setup_intake(tmp_path, free=True)
    command = upload_command(h)
    original = fail_after_blob(h, monkeypatch)
    with pytest.raises(RuntimeError):
        h.intake.upload(h.owner_actor, h.first.id, command)
    charge = held(h)[0]
    altered = command.model_copy(
        update={"content_base64": base64.b64encode(b"Changed content.").decode()}
    )
    with pytest.raises(IdempotencyConflictError):
        h.intake.upload(h.owner_actor, h.first.id, altered)
    monkeypatch.setattr(h.store, "insert_native_intake_source", original)
    configure(h, background="Changed intake requirements.")
    with pytest.raises(InvalidTransitionError):
        h.intake.upload(h.owner_actor, h.first.id, command)
    assert held(h) == [charge]
    reconcile(h, charge)
    assert not held(h)
    assert not h.intake.source_bytes._path(h.workspace, h.first.id, charge.sha256).exists()
    with pytest.raises(InvalidTransitionError, match="new upload operation key"):
        h.intake.upload(h.owner_actor, h.first.id, command)


def test_quota_exhaustion_precedes_any_file_write_or_pending_claim(tmp_path, monkeypatch):
    h = setup_intake(tmp_path, free=True)
    quota(h, 0)

    def unexpected(*args, **kwargs):
        pytest.fail("Exhausted quota must reject before writing source bytes.")

    monkeypatch.setattr(h.intake.source_bytes, "put", unexpected)
    with pytest.raises(InvalidTransitionError):
        h.intake.upload(h.owner_actor, h.first.id, upload_command(h))
    assert not held(h)
    assert h.store.native_intake_sources(h.workspace, h.first.id) == ()
    assert not h.intake.source_bytes.root.exists()


def test_archived_overquota_owner_can_reconcile_and_replay_cleanup(tmp_path, monkeypatch):
    h = setup_intake(tmp_path, free=True)
    fail_after_blob(h, monkeypatch)
    with pytest.raises(RuntimeError):
        h.intake.upload(h.owner_actor, h.first.id, upload_command(h))
    charge = held(h)[0]
    project = h.store.native_project(h.workspace, h.first.id)
    h.projects.update_project(
        h.owner_actor,
        h.first.id,
        UpdateNativeProject(
            expected_version=project.version,
            name=project.name,
            objective=project.objective,
            status="archived",
            idempotency_key=str(uuid4()),
        ),
    )
    quota(h, 0)
    state = h.intake.view(h.owner_actor, h.first.id)
    assert state["can_manage"] is False and state["can_reconcile_uploads"] is True
    assert state["pending_uploads"]
    command = ReconcileSourceUpload(
        expected_version=state["intake"]["version"],
        reason="Clean up archived work.",
        idempotency_key=str(uuid4()),
    )
    result = reconcile(h, charge, command=command)
    assert result["released_bytes"] == charge.bytes
    assert reconcile(h, charge, command=command) == result
    assert not held(h)


@pytest.mark.parametrize("reverse", [False, True])
def test_two_stale_shared_claims_release_in_either_order_and_last_deletes_blob(
    tmp_path, monkeypatch, reverse
):
    h = setup_intake(tmp_path, free=True)
    fail_after_blob(h, monkeypatch)
    commands = [upload_command(h, idempotency_key=str(uuid4())) for _ in range(2)]
    for command in commands:
        with pytest.raises(RuntimeError):
            h.intake.upload(h.owner_actor, h.first.id, command)
    charges = held(h)
    assert len(charges) == 2 and charges[0].sha256 == charges[1].sha256
    configure(h, outcomes="Fresh requirements make both old uploads stale.")
    if reverse:
        charges.reverse()
    path = h.intake.source_bytes._path(h.workspace, h.first.id, charges[0].sha256)
    reconcile(h, charges[0])
    assert path.exists() and len(held(h)) == 1
    reconcile(h, charges[1])
    assert not path.exists() and not held(h)


def test_shared_retained_revoked_source_keeps_blob_when_other_claim_is_released(
    tmp_path, monkeypatch
):
    h = setup_intake(tmp_path, free=True)
    raw = b"Shared immutable original."
    source = h.intake.upload(h.owner_actor, h.first.id, upload_command(h, raw))
    h.store.revoke_native_intake_source(h.workspace, h.first.id, source.id, source.created_at)
    fail_after_blob(h, monkeypatch)
    with pytest.raises(RuntimeError):
        h.intake.upload(h.owner_actor, h.first.id, upload_command(h, raw))
    charge = held(h)[0]
    reconcile(h, charge)
    assert h.intake.source_bytes.read(h.workspace, h.first.id, source.sha256, len(raw)) == raw
    assert h.store.storage_charge(h.workspace, "source", source.id).state == "retained"


def test_crash_between_blob_delete_and_release_keeps_hold_for_safe_retry(tmp_path, monkeypatch):
    h = setup_intake(tmp_path, free=True)
    fail_after_blob(h, monkeypatch)
    with pytest.raises(RuntimeError):
        h.intake.upload(h.owner_actor, h.first.id, upload_command(h))
    charge = held(h)[0]
    release = h.store.release_source_storage

    def interrupted(*args):
        raise SystemExit("Synthetic interruption after filesystem cleanup.")

    monkeypatch.setattr(h.store, "release_source_storage", interrupted)
    with pytest.raises(SystemExit):
        reconcile(h, charge)
    assert held(h) == [charge]
    assert not h.intake.source_bytes._path(h.workspace, h.first.id, charge.sha256).exists()
    monkeypatch.setattr(h.store, "release_source_storage", release)
    reconcile(h, charge)
    assert not held(h)


def test_known_owned_staging_is_cleaned_but_foreign_staging_and_corrupt_blob_are_protected(
    tmp_path,
):
    h = setup_intake(tmp_path, free=True)
    raw = b"Interrupted original."
    sha = hashlib.sha256(raw).hexdigest()
    source_id = uuid4()
    charge = StorageCharge(
        workspace_id=h.workspace,
        project_id=h.first.id,
        object_kind="source",
        object_id=source_id,
        bytes=len(raw),
        sha256=sha,
        request_fingerprint="a" * 64,
    )
    h.store.reserve_source_storage(charge)
    path = h.intake.source_bytes._path(h.workspace, h.first.id, sha)
    path.parent.mkdir(parents=True)
    owned = path.parent / f".upload-{source_id}-partial"
    foreign = path.parent / f".upload-{uuid4()}-partial"
    owned.write_bytes(raw[:5])
    foreign.write_bytes(b"Other pending bytes.")
    reconcile(h, charge)
    assert not owned.exists() and foreign.exists()
    charge = charge.model_copy(update={"object_id": uuid4()})
    h.store.reserve_source_storage(charge)
    path.write_bytes(b"Tampered blob.")
    with pytest.raises(ValidationError, match="integrity"):
        reconcile(h, charge)
    assert path.exists() and held(h) == [charge]


def test_zero_byte_pending_claims_cannot_bypass_project_count_capacity(tmp_path, monkeypatch):
    h = setup_intake(tmp_path, free=True)
    pending = tuple(
        StorageCharge(
            workspace_id=h.workspace,
            project_id=h.first.id,
            object_kind="source",
            object_id=uuid4(),
            bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            request_fingerprint="a" * 64,
        )
        for _ in range(2000)
    )
    real_charges = h.store.storage_charges

    def inventory(workspace_id, project_id=None):
        if workspace_id == h.workspace and project_id in {None, h.first.id}:
            return pending
        return real_charges(workspace_id, project_id)

    monkeypatch.setattr(h.store, "storage_charges", inventory)
    with pytest.raises(ValidationError, match="capacity"):
        h.intake.upload(h.owner_actor, h.first.id, upload_command(h, b""))
    assert len(held(h)) == 2000
    assert real_charges(h.workspace) == ()
    assert not h.intake.source_bytes.root.exists()


def test_reconciliation_rechecks_owner_and_project_scope_and_retained_is_protected(
    tmp_path, monkeypatch
):
    h = setup_intake(tmp_path, free=True)
    source = h.intake.upload(h.owner_actor, h.first.id, upload_command(h))
    with pytest.raises(InvalidTransitionError, match="retained"):
        reconcile(h, h.store.storage_charge(h.workspace, "source", source.id))
    fail_after_blob(h, monkeypatch)
    with pytest.raises(RuntimeError):
        h.intake.upload(h.owner_actor, h.first.id, upload_command(h))
    charge = held(h)[0]
    command = ReconcileSourceUpload(
        expected_version=0, reason="Try foreign cleanup.", idempotency_key=str(uuid4())
    )
    with pytest.raises(NotFoundError):
        h.intake.reconcile_upload(h.owner_actor, h.second.id, charge.object_id, command)
    h.store.delete_membership(h.owner, h.workspace)
    with pytest.raises(AuthorizationError):
        reconcile(h, charge)
    assert held(h) == [charge]
