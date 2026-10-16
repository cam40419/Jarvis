"""Portable delivery evidence, immutable history and live permission/applicability fences."""

import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import utc_now
from simon.domain.native_artifact_commands import UpdateArtifactReviewPolicy
from simon.domain.native_artifacts import NativeArtifactCheck
from simon.domain.native_delivery import CreateDeliveryPacket, DeliveryComponentInput
from simon.domain.native_intake import MAX_INTAKE_SOURCE_REVISIONS
from simon.services.native_delivery import NativeDeliveryService
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifacts import (
    current_revision,
    decide,
    detail,
    key,
    review,
    setup_artifacts,
    upload,
)
from tests.unit.test_native_execution import new_task


def setup_delivery():
    h = setup_artifacts()
    h.delivery = NativeDeliveryService(h.store, h.artifacts)
    h.policy = h.artifacts.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateArtifactReviewPolicy(
            expected_version=h.policy["version"],
            require_agent_review=False,
            reviewer_agent_id=h.reviewer.id,
            **key(),
        ),
    ).model_dump(mode="json")
    return h


def component(h, artifact, role="source", **changes):
    revision = current_revision(detail(h, artifact))
    return DeliveryComponentInput(
        artifact_id=artifact["id"],
        revision_id=revision["id"],
        sha256=revision["sha256"],
        role=role,
        **changes,
    )


def capture(h, *artifacts, **changes):
    command = CreateDeliveryPacket(
        title="Production handoff",
        components=tuple(component(h, a) for a in artifacts),
        **{**key(), **changes},
    )
    return h.delivery.create(h.owner_actor, h.first.id, command), command


def unpack(h, value):
    packet, raw = h.delivery.download(h.owner_actor, h.first.id, UUID(value["packet"]["id"]))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    return packet, raw, files


def test_packet_preserves_editable_originals_safe_previews_checks_decisions_and_limitations():
    h = setup_delivery()
    original = b"<script>window.bad=true</script>\r\n<title>Exact original</title>\r\n"
    first, _ = upload(h, original, filename="design.html", media_type="text/html")
    second, _ = upload(
        h,
        b'{"measurement": 42}',
        task=new_task(h),
        filename="spec.json",
        media_type="application/json",
    )
    decide(h, first)
    decide(h, second)
    value, _ = capture(h, first, second, limitations=("Physical sample remains unverified.",))
    packet, raw, files = unpack(h, value)
    assert value["applicability"] == {"current": True, "components_accepted": True, "reasons": []}
    assert hashlib.sha256(files["manifest.json"]).hexdigest() == packet.manifest_sha256
    paths = json.loads(files["export-paths.json"])
    assert files[paths[str(packet.manifest.components[0].revision_id)]] == original
    assert all(c.checks and c.decisions for c in packet.manifest.components)
    preview = files[packet.manifest.previews[0].path]
    assert b"&lt;script&gt;window.bad=true&lt;/script&gt;" in preview
    assert b"<script>" not in preview and b"default-src 'none'" in preview
    assert hashlib.sha256(preview).hexdigest() == packet.manifest.previews[0].content_sha256
    assert "Physical sample remains unverified." in packet.manifest.limitations
    assert (
        b"content_base64" not in files["manifest.json"]
        and b'"context"' not in files["manifest.json"]
    )
    assert h.delivery.download(h.owner_actor, h.first.id, packet.id)[1] == raw
    assert not h.calls


def test_snapshot_capture_does_not_approve_components_or_complete_tasks():
    h = setup_delivery()
    artifact, _ = upload(h)
    value, _ = capture(h, artifact)
    assert value["applicability"]["current"]
    assert not value["applicability"]["components_accepted"]
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status == "in_review"
    assert not value["packet"]["manifest"]["components"][0]["decisions"]


def test_later_revision_marks_packet_stale_preserves_original_and_unrelated_acceptance():
    h = setup_delivery()
    first, _ = upload(h, b"Original production requirement.")
    unrelated, _ = upload(h, b"Unchanged logo.", task=new_task(h))
    decide(h, first)
    decide(h, unrelated)
    stable = detail(h, unrelated)
    value, _ = capture(h, first, unrelated)
    packet, _, before = unpack(h, value)
    first, _ = upload(h, b"Corrected production requirement.", artifact=first)
    current = h.delivery.detail(h.owner_actor, h.first.id, packet.id)
    assert not current["applicability"]["current"]
    assert not current["applicability"]["components_accepted"]
    assert detail(h, unrelated) == stable
    _, _, after = unpack(h, current)
    assert {k: v for k, v in before.items() if k != "current-status.json"} == {
        k: v for k, v in after.items() if k != "current-status.json"
    }
    replacement, _ = capture(h, first, unrelated, previous_packet_id=packet.id)
    assert replacement["packet"]["sequence"] == 2
    assert replacement["applicability"]["current"]
    assert replacement["packet"]["manifest_sha256"] != packet.manifest_sha256


def test_idempotent_capture_and_receipt_survive_changed_head_but_payload_reuse_is_rejected():
    h = setup_delivery()
    artifact, _ = upload(h)
    value, command = capture(h, artifact)
    upload(h, b"Later contents.", artifact=artifact)
    replay = h.delivery.create(h.owner_actor, h.first.id, command)
    assert replay["packet"] == value["packet"]
    assert not replay["applicability"]["current"]
    assert h.delivery.operation_receipt(h.owner_actor, h.first.id, command.idempotency_key) == {
        "packet_id": value["packet"]["id"]
    }
    with pytest.raises(IdempotencyConflictError):
        h.delivery.create(
            h.owner_actor, h.first.id, command.model_copy(update={"title": "Different intent"})
        )
    assert len(h.store.delivery_packets(h.workspace, h.first.id)) == 1


def test_capture_rejects_changed_or_cross_project_component_and_rolls_back_receipt():
    h = setup_delivery()
    artifact, _ = upload(h)
    command = CreateDeliveryPacket(
        title="Exact files", components=(component(h, artifact),), **key()
    )
    upload(h, b"Changed since selection.", artifact=artifact)
    with pytest.raises(InvalidTransitionError):
        h.delivery.create(h.owner_actor, h.first.id, command)
    with pytest.raises(NotFoundError):
        h.delivery.operation_receipt(h.owner_actor, h.first.id, command.idempotency_key)
    with pytest.raises((NotFoundError, AuthorizationError)):
        h.delivery.create(h.owner_actor, h.second.id, command)
    assert h.store.delivery_packets(h.workspace, h.first.id) == ()


def test_concurrent_successors_have_one_winner_and_replay_recovers_it():
    h = setup_delivery()
    artifact, _ = upload(h)
    first, _ = capture(h, artifact)
    commands = [
        CreateDeliveryPacket(
            title=f"Revision {n}",
            components=(component(h, artifact),),
            previous_packet_id=first["packet"]["id"],
            **key(),
        )
        for n in range(2)
    ]

    def apply(command):
        try:
            return h.delivery.create(h.owner_actor, h.first.id, command)
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, commands))
    assert sum(result is not None for result in results) == 1
    for command, result in zip(commands, results, strict=True):
        if result:
            assert apply(command) == result
        else:
            with pytest.raises(NotFoundError):
                h.delivery.operation_receipt(h.owner_actor, h.first.id, command.idempotency_key)


def test_sources_beyond_excerpt_window_invalidate_packet_and_revocation_withholds_every_export():
    h = setup_delivery()
    sources = [source(h, source_key=f"source-{n}") for n in range(9)]
    for item in sources:
        h.store.insert_native_intake_source(item)
    artifact, _ = upload(h, b"Evidence derived from ninth source.")
    value, _ = capture(h, artifact)
    packet_id = UUID(value["packet"]["id"])
    h.store.insert_native_intake_source(
        source(h, source_key="source-8", revision=2, sha256="b" * 64)
    )
    assert not h.delivery.detail(h.owner_actor, h.first.id, packet_id)["applicability"]["current"]
    h.store.revoke_native_intake_source(h.workspace, h.first.id, sources[-1].id, utc_now())
    for operation in (h.delivery.detail, h.delivery.download):
        with pytest.raises(AuthorizationError):
            operation(h.owner_actor, h.first.id, packet_id)
    listing = h.delivery.view(h.owner_actor, h.first.id)
    assert listing["packets"]["items"][0]["disclosure"] == "withheld"
    assert "Production handoff" not in json.dumps(listing)


@pytest.mark.parametrize(
    "contents,media_type,limitation",
    [
        (bytes(range(256)), "application/octet-stream", "no validated preview"),
        (b"\xffinvalid", "text/plain", "not valid UTF-8"),
        (b"a" * 49_000, "text/plain", "truncated"),
    ],
    ids=["binary", "invalid_utf8", "truncated"],
)
def test_unsupported_or_truncated_preview_preserves_original_with_explicit_limits(
    contents, media_type, limitation
):
    h = setup_delivery()
    artifact, _ = upload(h, contents, media_type=media_type)
    value, _ = capture(h, artifact)
    packet, _, files = unpack(h, value)
    assert limitation in packet.manifest.previews[0].limitation
    assert limitation in " ".join(packet.manifest.limitations)
    assert files[next(iter(json.loads(files["export-paths.json"]).values()))] == contents


@pytest.mark.parametrize(
    "filename",
    ["CON.txt", "COM¹.txt", "two?files*.txt", "..leading.txt", "tail.", "🎉" * 225 + ".txt"],
    ids=["device", "unicode_device", "unsafe_characters", "dots", "trailing_dot", "long_unicode"],
)
def test_export_names_are_safe_and_keep_original_display_metadata(filename):
    h = setup_delivery()
    artifact, _ = upload(h, filename=filename)
    value, _ = capture(h, artifact)
    packet, _, files = unpack(h, value)
    assert packet.manifest.components[0].filename == filename
    path = next(iter(json.loads(files["export-paths.json"]).values()))
    assert not any(c in path for c in '?*:<>|"\\')
    assert ".." not in path.split("/")
    assert not path.endswith(".") and not path.endswith("/CON.txt")
    assert len(path.rsplit("/", 1)[-1].encode("utf-8")) <= 180
    assert not path.endswith("/COM¹.txt")
    if filename.endswith(".txt"):
        assert path.endswith(".txt")


def test_generated_preview_mismatch_fails_closed(monkeypatch):
    h = setup_delivery()
    artifact, _ = upload(h)
    value, _ = capture(h, artifact)
    from simon.services import native_delivery

    original = native_delivery.render_preview

    def changed(*args):
        preview, raw = original(*args)
        return preview.model_copy(update={"content_sha256": "f" * 64}), raw

    monkeypatch.setattr(native_delivery, "render_preview", changed)
    with pytest.raises(InvalidTransitionError, match="renderer"):
        unpack(h, value)


def test_failed_insert_keeps_no_packet_or_replay_receipt(monkeypatch):
    h = setup_delivery()
    artifact, _ = upload(h)
    command = CreateDeliveryPacket(
        title="Crash test", components=(component(h, artifact),), **key()
    )
    original = h.store.insert_delivery_packet

    def interrupted(value):
        original(value)
        raise RuntimeError("Interrupted after storage before receipt commit")

    monkeypatch.setattr(h.store, "insert_delivery_packet", interrupted)
    with pytest.raises(RuntimeError):
        h.delivery.create(h.owner_actor, h.first.id, command)
    assert not h.store.delivery_packets(h.workspace, h.first.id)
    with pytest.raises(NotFoundError):
        h.delivery.operation_receipt(h.owner_actor, h.first.id, command.idempotency_key)
    monkeypatch.setattr(h.store, "insert_delivery_packet", original)
    assert h.delivery.create(h.owner_actor, h.first.id, command)["packet"]["sequence"] == 1


def test_independent_pagination_and_empty_missing_receipt():
    h = setup_delivery()
    first, _ = upload(h)
    second, _ = upload(h, task=new_task(h))
    capture(h, first)
    capture(h, second)
    listing = h.delivery.view(h.owner_actor, h.first.id, 0, 1, 1, 1)
    assert len(listing["packets"]["items"]) == len(listing["artifacts"]["items"]) == 1
    assert listing["packets"]["has_more"] and not listing["artifacts"]["has_more"]
    assert listing["artifacts"]["items"][0]["id"] == second["id"]
    with pytest.raises(NotFoundError):
        h.delivery.operation_receipt(h.owner_actor, h.first.id, str(uuid4()))


def test_later_check_invalidates_capture_even_when_artifact_status_is_unchanged():
    h = setup_delivery()
    artifact, _ = upload(h)
    value, _ = capture(h, artifact)
    original = value["packet"]["manifest"]["components"][0]
    h.store.insert_artifact_check(
        NativeArtifactCheck(
            workspace_id=h.workspace,
            project_id=h.first.id,
            revision_id=UUID(original["revision_id"]),
            sha256=original["sha256"],
            check_name="additional_format_check",
            status="passed",
            details={"verified": "extra"},
        )
    )
    assert detail(h, artifact)["status"] == original["status_at_creation"]
    changed = h.delivery.detail(h.owner_actor, h.first.id, UUID(value["packet"]["id"]))
    assert not changed["applicability"]["current"]
    assert any("newer check" in reason for reason in changed["applicability"]["reasons"])
    assert changed["packet"] == value["packet"]


def test_new_review_evidence_and_mutable_review_state_are_bound_to_the_snapshot():
    h = setup_delivery()
    artifact, _ = upload(h)
    review(h, artifact, approved=False)
    value, _ = capture(h, artifact)
    first = value["packet"]["manifest"]["components"][0]
    assert first["review_states"][0]["status"] == "completed"
    review(h, artifact, approved=False)
    assert detail(h, artifact)["status"] == first["status_at_creation"]
    changed = h.delivery.detail(h.owner_actor, h.first.id, UUID(value["packet"]["id"]))
    assert not changed["applicability"]["current"]
    assert any(
        "review or decision evidence" in reason for reason in changed["applicability"]["reasons"]
    )


def test_source_authority_capacity_blocks_new_capture_but_keeps_old_packet_inspectable():
    h = setup_delivery()
    artifact, _ = upload(h)
    decide(h, artifact)
    value, _ = capture(h, artifact)
    for n in range(MAX_INTAKE_SOURCE_REVISIONS + 1):
        h.store.insert_native_intake_source(
            source(
                h,
                source_key="history.txt",
                revision=n + 1,
                size_bytes=0,
                text="",
                sha256=hashlib.sha256(b"").hexdigest(),
            )
        )
    changed = h.delivery.detail(h.owner_actor, h.first.id, UUID(value["packet"]["id"]))
    assert not changed["applicability"]["current"]
    assert not changed["applicability"]["components_accepted"]
    assert any("unknown" in reason for reason in changed["applicability"]["reasons"])
    assert unpack(h, changed)[2]["manifest.json"]
    summary = h.delivery.view(h.owner_actor, h.first.id)["packets"]["items"][0]["packet"]
    assert summary["manifest_sha256"] == value["packet"]["manifest_sha256"]
    assert summary["manifest"] == {"title": value["packet"]["manifest"]["title"]}
    with pytest.raises(ValidationError, match=str(MAX_INTAKE_SOURCE_REVISIONS)):
        capture(h, artifact)


def test_oversized_manifest_rejects_safely_without_record_or_receipt(monkeypatch):
    from simon.domain import native_delivery

    h = setup_delivery()
    artifact, _ = upload(h)
    command = CreateDeliveryPacket(
        title="Too much evidence", components=(component(h, artifact),), **key()
    )
    monkeypatch.setattr(native_delivery, "MAX_DELIVERY_MANIFEST_BYTES", 1)
    with pytest.raises(ValidationError, match="manifest bounds"):
        h.delivery.create(h.owner_actor, h.first.id, command)
    assert not h.store.delivery_packets(h.workspace, h.first.id)
    with pytest.raises(NotFoundError):
        h.delivery.operation_receipt(h.owner_actor, h.first.id, command.idempotency_key)
