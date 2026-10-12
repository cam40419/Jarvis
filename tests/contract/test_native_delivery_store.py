"""Immutable delivery packets preserve scoped exact revisions and evidence atomically."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_artifacts import (
    NativeArtifact,
    NativeArtifactCheck,
    NativeArtifactDecision,
    NativeReviewPolicy,
)
from simon.domain.native_delivery import (
    DeliveryComponent,
    DeliveryPreview,
    DeliveryReviewState,
    NativeDeliveryManifest,
    NativeDeliveryPacket,
    manifest_bytes,
)
from simon.domain.native_projects import NativeProjectMember
from tests.contract.test_native_artifact_store import review_record, revision_record
from tests.contract.test_native_execution_store import seed_execution
from tests.contract.test_native_intake_store import source


def seed_delivery(store):
    h = seed_execution(store)
    h.reviewer = h.agent.model_copy(update={"id": uuid4(), "role_key": "reviewer"})
    store.insert_native_agent(h.reviewer)
    h.artifact = NativeArtifact(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        name="Editable production source",
        created_by_agent_id=h.agent.id,
    )
    store.insert_artifact(h.artifact)
    h.revision = revision_record(h)
    store.insert_artifact_revision(h.revision)
    h.artifact = h.artifact.model_copy(update={"version": 2, "current_revision_id": h.revision.id})
    store.update_artifact(h.artifact, 1)
    h.check = NativeArtifactCheck(
        workspace_id=h.workspace,
        project_id=h.first.id,
        revision_id=h.revision.id,
        sha256=h.revision.sha256,
        check_name="content_integrity",
        status="passed",
        details={"actual_size_bytes": h.revision.size_bytes},
    )
    store.insert_artifact_check(h.check)
    h.review = review_record(h)
    store.insert_artifact_review(h.review)
    h.decision = NativeArtifactDecision(
        workspace_id=h.workspace,
        project_id=h.first.id,
        artifact_id=h.artifact.id,
        revision_id=h.revision.id,
        sha256=h.revision.sha256,
        policy_version=0,
        task_version=h.task.version,
        decided_by=h.owner,
        decision="approved",
    )
    store.insert_artifact_decision(h.decision)
    h.source = source(h)
    store.insert_native_intake_source(h.source)
    h.component = DeliveryComponent(
        artifact_id=h.artifact.id,
        revision_id=h.revision.id,
        sha256=h.revision.sha256,
        role="source",
        filename=h.revision.filename,
        media_type=h.revision.media_type,
        size_bytes=h.revision.size_bytes,
        artifact_version=h.artifact.version,
        task_version=h.task.version,
        status_at_creation="accepted",
        checks=(h.check,),
        decisions=(h.decision,),
        review_ids=(h.review.id,),
        review_states=(
            DeliveryReviewState(
                id=h.review.id,
                version=h.review.version,
                status=h.review.status,
                verdict=h.review.verdict,
            ),
        ),
    )
    return h


@pytest.fixture
def delivery(store):
    return seed_delivery(store)


def packet_record(h, component=None, manifest_changes=None, **changes):
    component = component or h.component
    generated = b"<p>Safe source preview</p>"
    manifest = NativeDeliveryManifest(
        **{
            "title": "Production handoff",
            "components": (component,),
            "previews": (
                DeliveryPreview(
                    revision_id=component.revision_id,
                    sha256=component.sha256,
                    path=f"previews/{component.revision_id}.html",
                    content_sha256=hashlib.sha256(generated).hexdigest(),
                    size_bytes=len(generated),
                    supported=True,
                ),
            ),
            "source_revision_ids": (h.source.id,),
            "source_fingerprint": "e" * 64,
            "project_version": h.first.version,
            "policy_version": 0,
            **(manifest_changes or {}),
        }
    )
    return NativeDeliveryPacket(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "created_by": h.owner,
            "sequence": 1,
            "manifest": manifest,
            "manifest_sha256": hashlib.sha256(manifest_bytes(manifest)).hexdigest(),
            **changes,
        }
    )


def test_packets_scope_pagination_exact_evidence_and_no_artifact_state_mutation(delivery):
    h = delivery
    first = packet_record(h)
    second = packet_record(
        h,
        sequence=2,
        previous_packet_id=first.id,
        created_at=first.created_at + timedelta(seconds=1),
    )
    for packet in (first, second):
        h.store.insert_delivery_packet(packet)
    assert h.store.delivery_packet(h.workspace, h.first.id, first.id) == first
    assert h.store.delivery_packet_successor(h.workspace, h.first.id, first.id) == second
    assert h.store.delivery_packet_successor(h.workspace, h.first.id, second.id) is None
    assert h.store.delivery_packets(h.workspace, h.first.id, limit=1) == (first,)
    assert h.store.delivery_packets(h.workspace, h.first.id) == (first, second)
    assert h.store.delivery_packets(h.workspace, h.first.id, 1, 1) == (second,)
    assert h.store.delivery_packets(h.workspace, h.first.id, 2, 1) == ()
    for ws, pid in ((h.workspace, h.second.id), (h.foreign_workspace, h.first.id)):
        assert h.store.delivery_packet(ws, pid, first.id) is None
        assert h.store.delivery_packet_successor(ws, pid, first.id) is None
        assert h.store.delivery_packets(ws, pid) == ()
    assert h.store.artifact(h.workspace, h.first.id, h.artifact.id) == h.artifact
    assert h.store.native_task(h.workspace, h.first.id, h.task.id) == h.task
    assert first.manifest.components[0].checks == (h.check,)
    assert first.manifest.components[0].decisions == (h.decision,)
    first.manifest.components[0].checks[0].details["actual_size_bytes"] = -1
    read = h.store.delivery_packet(h.workspace, h.first.id, first.id)
    assert (
        read.manifest.components[0].checks[0].details["actual_size_bytes"] == h.revision.size_bytes
    )
    read.manifest.components[0].checks[0].details.clear()
    assert h.store.delivery_packet(h.workspace, h.first.id, first.id).manifest_sha256 == (
        hashlib.sha256(
            manifest_bytes(h.store.delivery_packet(h.workspace, h.first.id, first.id).manifest)
        ).hexdigest()
    )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(second)


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_id": uuid4()},
        {"revision_id": uuid4()},
        {"filename": "different.txt"},
        {"media_type": "application/json"},
        {"size_bytes": 0},
        {"sha256": "a" * 64},
        {"artifact_version": 3},
        {"task_version": 2},
    ],
)
def test_component_admission_requires_current_scoped_exact_content(delivery, changes):
    h = delivery
    # Remove included evidence so missing revision tests reach persisted reference validation.
    component = h.component.model_copy(update={"checks": (), "decisions": (), **changes})
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h, component))


def test_evidence_references_cannot_invent_or_rewrite_recorded_outcomes(delivery):
    h = delivery
    unknown = DeliveryReviewState(id=uuid4(), version=1, status="running")
    for changes in (
        {"review_ids": (unknown.id,), "review_states": (unknown,)},
        {"review_states": (h.component.review_states[0].model_copy(update={"version": 2}),)},
        {"checks": (h.check.model_copy(update={"details": {"actual_size_bytes": 0}}),)},
        {"checks": (h.check.model_copy(update={"id": uuid4()}),)},
        {"decisions": (h.decision.model_copy(update={"feedback": "Invented approval"}),)},
        {"decisions": (h.decision.model_copy(update={"id": uuid4()}),)},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_delivery_packet(packet_record(h, h.component.model_copy(update=changes)))
    wrong_review = review_record(h, idempotency_key="review-wrong-hash", sha256="a" * 64)
    # Direct review writes already reject hash mismatch; packet IDs cannot bypass that authority.
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(wrong_review)


def test_review_settlement_preserves_manifest_and_invalidates_old_capture_state(delivery):
    h = delivery
    original = packet_record(h)
    h.store.insert_delivery_packet(original)
    completed = h.review.model_copy(
        update={
            "version": 2,
            "status": "completed",
            "verdict": "approved",
            "finished_at": utc_now(),
        }
    )
    h.store.update_artifact_review(completed, 1)
    assert h.store.delivery_packet(h.workspace, h.first.id, original.id) == original
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h))
    component = h.component.model_copy(
        update={
            "review_states": (
                DeliveryReviewState(
                    id=h.review.id, version=2, status="completed", verdict="approved"
                ),
            )
        }
    )
    successor = packet_record(h, component, sequence=2, previous_packet_id=original.id)
    h.store.insert_delivery_packet(successor)
    assert h.store.delivery_packet(h.workspace, h.first.id, successor.id) == successor


def test_creator_admission_and_intake_reference_revocation_preserve_history(delivery):
    h = delivery
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h, created_by=h.member))
    h.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=h.workspace,
            project_id=h.first.id,
            actor_id=h.member,
        )
    )
    first = packet_record(h, created_by=h.member)
    h.store.insert_delivery_packet(first)
    h.store.delete_native_project_member(h.workspace, h.first.id, h.member)
    assert h.store.delivery_packet(h.workspace, h.first.id, first.id) == first
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h, created_by=h.member))
    for changes in (
        {"source_revision_ids": (uuid4(),)},
        {"project_version": 2},
        {"policy_version": 1},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_delivery_packet(packet_record(h, manifest_changes=changes))
    h.store.revoke_native_intake_source(h.workspace, h.first.id, h.source.id, utc_now())
    assert h.store.delivery_packet(h.workspace, h.first.id, first.id) == first
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h))
    h.store.delete_membership(h.member, h.workspace)
    assert h.store.delivery_packet(h.workspace, h.first.id, first.id) == first


def test_workspace_owner_bypass_and_other_project_source_isolation(delivery):
    h = delivery
    h.store.delete_native_project_member(h.workspace, h.first.id, h.owner)
    valid = packet_record(h)
    h.store.insert_delivery_packet(valid)
    foreign_source = source(h, project_id=h.second.id)
    h.store.insert_native_intake_source(foreign_source)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(
            packet_record(
                h,
                manifest_changes={"source_revision_ids": (foreign_source.id,)},
            )
        )
    h.store.delete_membership(h.owner, h.workspace)
    assert h.store.delivery_packet(h.workspace, h.first.id, valid.id) == valid
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h))


def test_changed_project_policy_and_task_versions_fence_new_capture(delivery):
    h = delivery
    captured = packet_record(h)
    h.store.insert_delivery_packet(captured)
    h.store.update_native_task(h.task.model_copy(update={"version": 2, "title": "Changed"}), 1)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h))
    current_component = h.component.model_copy(update={"task_version": 2})
    h.store.save_review_policy(
        NativeReviewPolicy(
            workspace_id=h.workspace,
            project_id=h.first.id,
            version=1,
        ),
        0,
    )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h, current_component))
    h.store.update_native_project(h.first.model_copy(update={"version": 2, "name": "Changed"}), 1)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(
            packet_record(
                h,
                current_component,
                manifest_changes={"policy_version": 1},
            )
        )
    assert h.store.delivery_packet(h.workspace, h.first.id, captured.id) == captured


def test_capture_requires_current_head_but_preserves_previous_packet(delivery):
    h = delivery
    captured = packet_record(h)
    h.store.insert_delivery_packet(captured)
    next_revision = revision_record(h, sequence=2, parent_revision_id=h.revision.id)
    h.store.insert_artifact_revision(next_revision)
    next_artifact = h.artifact.model_copy(
        update={
            "version": 3,
            "current_revision_id": next_revision.id,
        }
    )
    h.store.update_artifact(next_artifact, 2)
    # Current version alone cannot turn a historical revision into the current source.
    historical = h.component.model_copy(update={"artifact_version": 3})
    with pytest.raises(InvalidTransitionError):
        h.store.insert_delivery_packet(packet_record(h, historical))
    current = historical.model_copy(
        update={
            "revision_id": next_revision.id,
            "checks": (),
            "decisions": (),
            "review_ids": (),
            "review_states": (),
            "status_at_creation": "in_review",
        }
    )
    successor = packet_record(h, current, sequence=2, previous_packet_id=captured.id)
    h.store.insert_delivery_packet(successor)
    assert h.store.delivery_packet(h.workspace, h.first.id, captured.id) == captured
    assert h.store.delivery_packet(h.workspace, h.first.id, successor.id) == successor


def test_successor_is_scoped_sequential_and_one_concurrent_writer_wins(delivery):
    h = delivery
    parent = packet_record(h)
    h.store.insert_delivery_packet(parent)
    for changes in (
        {"previous_packet_id": uuid4(), "sequence": 2},
        {"previous_packet_id": parent.id, "sequence": 3},
        {"previous_packet_id": parent.id, "sequence": 2, "project_id": h.second.id},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_delivery_packet(packet_record(h).model_copy(update=changes))
    candidates = tuple(packet_record(h, sequence=2, previous_packet_id=parent.id) for _ in range(2))

    def insert(packet):
        try:
            h.store.insert_delivery_packet(packet)
            return True
        except InvalidTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(insert, candidates))
    assert sum(outcomes) == 1
    rows = h.store.delivery_packets(h.workspace, h.first.id)
    assert len(rows) == 2 and rows[1].previous_packet_id == parent.id


def test_packet_and_idempotency_receipt_commit_and_roll_back_together(delivery):
    h = delivery
    packet = packet_record(h)
    namespace = f"delivery:{h.workspace}:{h.first.id}"

    def create():
        h.store.insert_delivery_packet(packet)
        return {"packet_id": str(packet.id), "manifest_sha256": packet.manifest_sha256}

    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.execute_once(namespace, "packet-publication", "a" * 64, create)
        raise RuntimeError("Injected delivery publication failure")
    assert h.store.delivery_packet(h.workspace, h.first.id, packet.id) is None
    assert h.store.command_receipt(namespace, "packet-publication") is None
    result, replayed = h.store.execute_once(namespace, "packet-publication", "a" * 64, create)
    assert not replayed and result["packet_id"] == str(packet.id)
    replay, replayed = h.store.execute_once(namespace, "packet-publication", "a" * 64, create)
    assert replayed and replay == result
    assert h.store.delivery_packets(h.workspace, h.first.id) == (packet,)


@pytest.mark.postgres
def test_postgres_direct_delivery_rewrite_and_unscoped_inserts_are_rejected(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore
    from simon.seed import seed_development_identity

    seed_development_identity(postgres_url)
    store = PostgresStore(postgres_url)
    try:
        h = seed_delivery(store)
        packet = packet_record(h)
        store.insert_delivery_packet(packet)
        for operation in (
            "UPDATE native_delivery_packets SET snapshot=snapshot WHERE id=%s",
            "DELETE FROM native_delivery_packets WHERE id=%s",
        ):
            with pytest.raises(psycopg.IntegrityError), store.transaction():
                store.connection.execute(operation, (packet.id,))
        for value in (
            packet.model_copy(update={"id": uuid4(), "project_id": h.second.id}),
            packet_record(h, created_by=h.member),
            packet.model_copy(update={"id": uuid4(), "manifest_sha256": "0" * 64}),
            packet_record(h, h.component.model_copy(update={"filename": "different.txt"})),
            packet_record(
                h,
                h.component.model_copy(
                    update={
                        "review_states": (
                            h.component.review_states[0].model_copy(update={"version": 2}),
                        )
                    }
                ),
            ),
            packet_record(h, manifest_changes={"source_revision_ids": (uuid4(),)}),
            packet_record(h, sequence=3, previous_packet_id=packet.id),
        ):
            with pytest.raises(InvalidTransitionError):
                store._artifact_write("native_delivery_packets", value, None)
        for changes in (
            {"previews": ()},
            {"components": (h.component, h.component)},
            {"components": (h.component.model_copy(update={"review_states": ()}),)},
            {
                "components": (
                    h.component.model_copy(
                        update={
                            "checks": (
                                h.check.model_copy(update={"details": {"actual_size_bytes": 0}}),
                            )
                        }
                    ),
                )
            },
        ):
            manifest = packet.manifest.model_copy(update=changes)
            unchecked = packet.model_copy(
                update={
                    "id": uuid4(),
                    "manifest": manifest,
                    "manifest_sha256": hashlib.sha256(manifest_bytes(manifest)).hexdigest(),
                }
            )
            with pytest.raises(InvalidTransitionError):
                store._artifact_write("native_delivery_packets", unchecked, None)
        assert store.delivery_packet(h.workspace, h.first.id, packet.id) == packet
    finally:
        store.close()


@pytest.mark.postgres
def test_postgres_legacy_decision_default_can_be_captured_without_rewriting_history(postgres_url):
    from simon.adapters.postgres import PostgresStore
    from simon.seed import seed_development_identity

    seed_development_identity(postgres_url)
    store = PostgresStore(postgres_url)
    try:
        h = seed_delivery(store)
        legacy_id = uuid4()
        with store.transaction(h.workspace):
            store.connection.execute(
                "INSERT INTO native_artifact_decisions "
                "(id,workspace_id,project_id,artifact_id,revision_id,sha256,review_id,"
                "repair_task_id,decided_by,decision,created_at,snapshot) "
                "SELECT %s,workspace_id,project_id,artifact_id,revision_id,sha256,review_id,"
                "repair_task_id,decided_by,decision,created_at,"
                "(snapshot-'authority_snapshot')||jsonb_build_object('id',%s::uuid) "
                "FROM native_artifact_decisions WHERE id=%s",
                (legacy_id, legacy_id, h.decision.id),
            )
            original_snapshot = store.connection.execute(
                "SELECT snapshot FROM native_artifact_decisions WHERE id=%s",
                (legacy_id,),
            ).fetchone()["snapshot"]
        assert "authority_snapshot" not in original_snapshot
        legacy = next(
            d
            for d in store.artifact_decisions(
                h.workspace,
                h.first.id,
                h.revision.id,
            )
            if d.id == legacy_id
        )
        assert legacy.authority_snapshot == {}
        packet = packet_record(
            h,
            h.component.model_copy(
                update={
                    "decisions": (h.decision, legacy),
                    "status_at_creation": "in_review",
                }
            ),
        )
        store.insert_delivery_packet(packet)
        assert store.delivery_packet(h.workspace, h.first.id, packet.id) == packet
        with store.transaction():
            after = store.connection.execute(
                "SELECT snapshot FROM native_artifact_decisions WHERE id=%s",
                (legacy_id,),
            ).fetchone()["snapshot"]
        assert after == original_snapshot
    finally:
        store.close()
