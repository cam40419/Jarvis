"""Retention accounting across artifact, delivery and resumable execution workflows."""

from datetime import timedelta
from uuid import UUID

import pytest

from simon.domain.errors import StorageQuotaExceededError
from simon.domain.models import utc_now
from simon.domain.native_delivery import manifest_bytes
from simon.domain.native_storage import StorageEntitlement
from tests.unit.test_native_artifacts import decide, detail, upload
from tests.unit.test_native_delivery import capture, setup_delivery, unpack
from tests.unit.test_native_execution import advance, claim, draft, retry, setup_execution, start


def limit(h, amount, **changes):
    previous = h.store.storage_entitlement(h.workspace)
    version = previous.version if previous else 0
    policy = StorageEntitlement(
        workspace_id=h.workspace,
        version=version + 1,
        plan_key="synthetic",
        limit_bytes=amount,
        issued_by=h.owner_actor.actor_id,
        reason="Deterministic resource boundary fixture",
        **changes,
    )
    h.store.save_storage_entitlement(policy, version)
    return policy


def test_delivery_charges_manifest_and_generated_previews_without_recharging_sources():
    h = setup_delivery()
    original = b"Editable source bytes"
    artifact, _ = upload(h, original)
    decide(h, artifact)
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == len(original)
    value, command = capture(h, artifact)
    packet, _, _ = unpack(h, value)
    expected = (
        len(original)
        + len(manifest_bytes(packet.manifest))
        + sum(p.size_bytes for p in packet.manifest.previews)
    )
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == expected
    limit(h, 0)
    assert h.delivery.create(h.owner_actor, h.first.id, command)["packet"]["id"] == str(packet.id)
    unpack(h, value)
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == expected
    with pytest.raises(StorageQuotaExceededError):
        capture(h, artifact, previous_packet_id=packet.id)
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == expected
    assert h.store.delivery_packet_successor(h.workspace, h.first.id, packet.id) is None


def test_artifact_denial_preserves_accepted_bytes_head_task_and_allowance():
    h = setup_delivery()
    artifact, _ = upload(h, b"Approved source")
    decide(h, artifact)
    before = detail(h, artifact)
    limit(h, len(b"Approved source"))
    with pytest.raises(StorageQuotaExceededError):
        upload(h, b"Denied new content", artifact=artifact)
    assert detail(h, artifact) == before
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == len(b"Approved source")


def test_expired_override_denies_new_admission_and_preserves_accepted_content():
    h = setup_delivery()
    policy = limit(
        h, 0, override_limit_bytes=1000, override_expires_at=utc_now() - timedelta(seconds=1)
    )
    assert policy.effective_limit(utc_now()) == 0
    with pytest.raises(StorageQuotaExceededError):
        upload(h)
    assert h.store.storage_usage(h.workspace)["used_bytes"] == 0


def test_quota_failure_keeps_paid_checkpoint_and_retry_publishes_without_another_model_call():
    h = setup_execution()
    limit(h, 0)
    run = start(h)
    result = advance(h, claim(h), draft("Retained completed checkpoint"))
    assert result["status"] == "failed" and result["error_code"] == "storage_quota_exceeded"
    assert len(h.calls) == 1
    steps = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))
    assert steps[0].status == "completed"
    assert steps[0].result["output"] == "Retained completed checkpoint"
    usage_before = h.store.model_usage_entries(h.workspace, h.first.id)
    assert usage_before and all(item.status == "settled" for item in usage_before)
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == 0
    limit(h, 1000)
    retry(h, result)
    completed = advance(h, claim(h))
    assert completed["status"] == "completed"
    assert len(h.calls) == 1
    assert h.store.model_usage_entries(h.workspace, h.first.id) == usage_before
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == len(
        b"Retained completed checkpoint"
    )
