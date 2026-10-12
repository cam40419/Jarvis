"""Portable manifests bind every preview and evidence snapshot to exact source bytes."""

import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.native_artifacts import NativeArtifactCheck
from simon.domain.native_delivery import (
    CreateDeliveryPacket,
    DeliveryComponent,
    DeliveryComponentInput,
    DeliveryPreview,
    DeliveryReviewState,
    NativeDeliveryManifest,
    NativeDeliveryPacket,
    manifest_bytes,
)


def component(**changes):
    return DeliveryComponent(
        **{
            "artifact_id": uuid4(),
            "revision_id": uuid4(),
            "sha256": "a" * 64,
            "role": "source",
            "filename": "Production specification.txt",
            "media_type": "text/plain",
            "size_bytes": 42,
            "artifact_version": 2,
            "task_version": 1,
            "status_at_creation": "in_review",
            **changes,
        }
    )


def preview(item, **changes):
    return DeliveryPreview(
        **{
            "revision_id": item.revision_id,
            "sha256": item.sha256,
            "path": f"previews/{item.revision_id}.html",
            "content_sha256": "b" * 64,
            "size_bytes": 20,
            "supported": True,
            **changes,
        }
    )


def manifest(item=None, **changes):
    item = item or component()
    return NativeDeliveryManifest(
        **{
            "title": "Café production handoff",
            "components": (item,),
            "previews": (preview(item),),
            "source_fingerprint": "c" * 64,
            "project_version": 1,
            "policy_version": 0,
            **changes,
        }
    )


def packet(value, **changes):
    return NativeDeliveryPacket(
        **{
            "workspace_id": uuid4(),
            "project_id": uuid4(),
            "created_by": uuid4(),
            "sequence": 1,
            "manifest": value,
            "manifest_sha256": hashlib.sha256(manifest_bytes(value)).hexdigest(),
            **changes,
        }
    )


def test_manifest_checksum_is_portable_exact_utf8_and_roundtrips():
    value = manifest(
        limitations=("Sample remains unverified.",),
        declared_limitations=("Sample remains unverified.",),
    )
    raw = manifest_bytes(value)
    assert "Café".encode() in raw
    assert (
        raw
        == json.dumps(
            value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    record = packet(value)
    restored = NativeDeliveryPacket.model_validate_json(record.model_dump_json())
    assert restored == record and manifest_bytes(restored.manifest) == raw
    with pytest.raises(ValidationError):
        packet(value, manifest_sha256="0" * 64)


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "preview"},
        {"derived_from_revision_id": uuid4()},
        {"role": "preview", "derived_from_revision_id": "self"},
        {"filename": "../../unsafe.txt"},
        {"media_type": "text/plain\r\nHeader"},
        {"artifact_version": True},
        {"size_bytes": 1024 * 1024 + 1},
    ],
)
def test_component_rejects_unsafe_metadata_and_unbound_preview(changes):
    if changes.get("derived_from_revision_id") == "self":
        changes = {**changes, "revision_id": uuid4()}
        changes["derived_from_revision_id"] = changes["revision_id"]
    with pytest.raises(ValidationError):
        component(**changes)


def test_command_requires_distinct_sources_and_internal_preview_derivation():
    original = component()
    source = DeliveryComponentInput.model_validate(
        original.model_dump(include=set(DeliveryComponentInput.model_fields))
    )
    derived = DeliveryComponentInput(
        artifact_id=uuid4(),
        revision_id=uuid4(),
        sha256="d" * 64,
        role="preview",
        derived_from_revision_id=source.revision_id,
    )
    command = CreateDeliveryPacket(
        idempotency_key="delivery-contract", title="Production", components=(source, derived)
    )
    assert command.components[1].derived_from_revision_id == source.revision_id
    for values in (
        (),
        (source, source),
        (derived,),
        (source, derived.model_copy(update={"derived_from_revision_id": uuid4()})),
        (source, source.model_copy(update={"revision_id": uuid4()})),
    ):
        with pytest.raises(ValidationError):
            CreateDeliveryPacket(
                idempotency_key="delivery-contract", title="Production", components=values
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "../preview.html"},
        {"path": "https://external.invalid/preview"},
        {"size_bytes": 512 * 1024 + 1},
        {"renderer": "untrusted"},
        {"supported": False},
        {"content_sha256": "A" * 64},
    ],
)
def test_generated_preview_is_internal_bounded_and_explicit_about_support(changes):
    with pytest.raises(ValidationError):
        preview(component(), **changes)
    unsupported = preview(component(), supported=False, limitation="Binary rendering unsupported.")
    assert not unsupported.supported


def test_manifest_requires_one_exact_preview_per_source_and_safe_limitations():
    original = component()
    generated = preview(original)
    for changes in (
        {"previews": ()},
        {"previews": (generated, generated)},
        {"previews": (generated.model_copy(update={"sha256": "d" * 64}),)},
        {"previews": (preview(component()),)},
        {"source_revision_ids": (original.revision_id, original.revision_id)},
        {"limitations": ("unsafe\x00limitation",)},
        {"declared_limitations": ("unsafe\ud800limitation",)},
        {"declared_limitations": ("Declared",) * 21},
    ):
        with pytest.raises(ValidationError):
            manifest(original, **changes)


def test_evidence_must_bind_exact_content_and_total_manifest_stays_bounded():
    original = component()
    check = NativeArtifactCheck(
        workspace_id=uuid4(),
        project_id=uuid4(),
        revision_id=original.revision_id,
        sha256=original.sha256,
        check_name="content_integrity",
        status="passed",
    )
    for changes in (
        {"checks": (check.model_copy(update={"sha256": "d" * 64}),)},
        {"checks": (check, check)},
        {"review_ids": (check.id, check.id)},
    ):
        with pytest.raises(ValidationError):
            component(artifact_id=original.artifact_id, revision_id=original.revision_id, **changes)
    large = original.model_copy(
        update={
            "checks": tuple(
                check.model_copy(update={"id": uuid4(), "details": {"evidence": "x" * 16000}})
                for _ in range(70)
            )
        }
    )
    with pytest.raises(ValidationError, match="bounded byte size"):
        manifest(large)


def test_review_states_must_capture_every_review_id_once():
    state = DeliveryReviewState(id=uuid4(), version=1, status="running")
    assert component(review_ids=(state.id,), review_states=(state,)).review_states == (state,)
    for changes in (
        {"review_ids": (state.id,)},
        {"review_states": (state,)},
        {"review_ids": (state.id,), "review_states": (state, state)},
    ):
        with pytest.raises(ValidationError):
            component(**changes)


def test_packet_enforces_nonbranching_sequence_shape_and_project_evidence():
    value = manifest()
    for changes in (
        {"sequence": 2},
        {"previous_packet_id": uuid4()},
        {"sequence": True},
        {"id": uuid4(), "previous_packet_id": "self", "sequence": 2},
    ):
        if changes.get("previous_packet_id") == "self":
            changes = {**changes, "previous_packet_id": changes["id"]}
        with pytest.raises(ValidationError):
            packet(value, **changes)
    original = value.components[0]
    check = NativeArtifactCheck(
        workspace_id=uuid4(),
        project_id=uuid4(),
        revision_id=original.revision_id,
        sha256=original.sha256,
        check_name="content_integrity",
        status="passed",
    )
    with pytest.raises(ValidationError, match="packet's project"):
        packet(manifest(original.model_copy(update={"checks": (check,)})))
