"""Workspace accounting admits exact retained bytes once and serializes quota contenders."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError, StorageQuotaExceededError
from simon.domain.identity import Membership
from simon.domain.models import utc_now
from simon.domain.native_storage import StorageCharge, StorageEntitlement
from tests.contract.test_native_delivery_store import packet_record, seed_delivery
from tests.contract.test_native_intake_store import source
from tests.contract.test_native_project_store import seed_native_projects


@pytest.fixture
def storage(store):
    return seed_native_projects(store)


def policy(h, limit=100, **changes):
    return StorageEntitlement(
        **{
            "workspace_id": h.workspace,
            "version": 1,
            "plan_key": "starter",
            "limit_bytes": limit,
            "reason": "Test allocation",
            "issued_by": h.owner,
            **changes,
        }
    )


def reservation(h, **changes):
    return StorageCharge(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "object_kind": "source",
            "object_id": uuid4(),
            "bytes": 32,
            "sha256": "a" * 64,
            "request_fingerprint": "b" * 64,
            **changes,
        }
    )


def test_entitlement_cas_history_downgrades_expiry_and_scope(storage):
    h = storage
    assert h.store.storage_entitlement(h.workspace) is None
    first = policy(h, 200)
    second = policy(
        h,
        0,
        version=2,
        override_limit_bytes=100,
        override_expires_at=utc_now() - timedelta(seconds=1),
    )
    h.store.save_storage_entitlement(first, 0)
    h.store.reserve_source_storage(reservation(h))
    h.store.save_storage_entitlement(second, 1)
    assert h.store.storage_entitlement(h.workspace) == second
    assert h.store.storage_entitlement_history(h.workspace) == (second, first)
    assert h.store.storage_entitlement_history(h.workspace, 1) == (second,)
    assert h.store.storage_entitlement_history(h.foreign_workspace) == ()
    with pytest.raises(StorageQuotaExceededError):
        h.store.reserve_source_storage(reservation(h, bytes=1))
    with pytest.raises(InvalidTransitionError):
        h.store.save_storage_entitlement(policy(h, version=2), 1)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.save_storage_entitlement(policy(h, version=3), 2)
        raise RuntimeError("Injected allocation failure")
    assert h.store.storage_entitlement(h.workspace) == second


def test_workspace_identity_survives_last_member_removal_and_creation_rolls_back(storage):
    h = storage
    assert h.store.workspace_exists(h.foreign_workspace)
    grant = policy(h, workspace_id=h.foreign_workspace)
    h.store.save_storage_entitlement(grant, 0)
    h.store.delete_membership(h.outsider, h.foreign_workspace)
    assert h.store.workspace_members(h.foreign_workspace, 0, 1) == ()
    assert h.store.workspace_exists(h.foreign_workspace)
    next_grant = grant.model_copy(update={"version": 2, "limit_bytes": 200})
    h.store.save_storage_entitlement(next_grant, 1)
    assert h.store.storage_entitlement_history(h.foreign_workspace) == (next_grant, grant)
    unknown = uuid4()
    assert not h.store.workspace_exists(unknown)
    with pytest.raises(InvalidTransitionError):
        h.store.save_storage_entitlement(policy(h, workspace_id=unknown), 0)
    with pytest.raises(RuntimeError), h.store.transaction(unknown):
        h.store.put_membership(Membership(actor_id=uuid4(), workspace_id=unknown, role="owner"))
        assert h.store.workspace_exists(unknown)
        raise RuntimeError("Injected identity creation failure")
    assert not h.store.workspace_exists(unknown)


def test_reservation_binding_retry_release_scope_and_usage(storage):
    h = storage
    value = reservation(h)
    h.store.reserve_source_storage(value)
    h.store.reserve_source_storage(value.model_copy(update={"created_at": utc_now()}))
    assert h.store.storage_charge(h.workspace, "source", value.object_id) == value
    assert h.store.storage_usage(h.workspace) == {
        "reserved_bytes": 32,
        "retained_bytes": 0,
        "used_bytes": 32,
        "reservation_count": 1,
    }
    assert h.store.storage_usage(h.workspace, h.second.id)["used_bytes"] == 0
    assert h.store.storage_charges(h.workspace, h.first.id) == (value,)
    assert h.store.storage_charge(h.foreign_workspace, "source", value.object_id) is None
    h.store.release_source_storage(h.foreign_workspace, value.object_id)
    assert h.store.storage_charge(h.workspace, "source", value.object_id) == value
    for changes in (
        {"bytes": 33},
        {"sha256": "c" * 64},
        {"project_id": h.second.id},
        {"request_fingerprint": "d" * 64},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.reserve_source_storage(value.model_copy(update=changes))
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.release_source_storage(h.workspace, value.object_id)
        raise RuntimeError("Injected release failure")
    assert h.store.storage_charge(h.workspace, "source", value.object_id) == value
    h.store.release_source_storage(h.workspace, value.object_id)
    h.store.release_source_storage(h.workspace, value.object_id)
    assert h.store.storage_charge(h.workspace, "source", value.object_id).state == "released"
    assert h.store.storage_usage(h.workspace)["used_bytes"] == 0
    with pytest.raises(InvalidTransitionError):
        h.store.reserve_source_storage(value)


def test_source_consumes_exact_utf8_hold_after_downgrade_and_zero_admission(storage):
    h = storage
    evidence = source(h, text="é🙂")
    amount = evidence.size_bytes + len(evidence.text.encode("utf-8"))
    held = reservation(h, object_id=evidence.id, bytes=amount, sha256=evidence.sha256)
    h.store.save_storage_entitlement(policy(h, amount), 0)
    h.store.reserve_source_storage(held)
    for changes in ({"size_bytes": evidence.size_bytes + 1}, {"sha256": "d" * 64}):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_native_intake_source(evidence.model_copy(update=changes))
    assert h.store.storage_charge(h.workspace, "source", evidence.id) == held
    h.store.save_storage_entitlement(policy(h, 0, version=2), 1)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.insert_native_intake_source(evidence)
        raise RuntimeError("Injected publication failure")
    assert h.store.native_intake_source(h.workspace, h.first.id, evidence.id) is None
    assert h.store.storage_charge(h.workspace, "source", evidence.id) == held
    h.store.insert_native_intake_source(evidence)
    retained = h.store.storage_charge(h.workspace, "source", evidence.id)
    assert retained == held.model_copy(update={"state": "retained"})
    assert h.store.storage_usage(h.workspace) == {
        "reserved_bytes": 0,
        "retained_bytes": amount,
        "used_bytes": amount,
        "reservation_count": 0,
    }
    with pytest.raises(InvalidTransitionError):
        h.store.release_source_storage(h.workspace, evidence.id)
    with pytest.raises(StorageQuotaExceededError):
        h.store.insert_native_intake_source(
            source(h, source_key="positive.txt", text="x", size_bytes=0)
        )
    zero = source(h, source_key="zero.txt", text="", size_bytes=0)
    h.store.insert_native_intake_source(zero)
    assert h.store.storage_charge(h.workspace, "source", zero.id).bytes == 0
    assert h.store.storage_usage(h.workspace)["used_bytes"] == amount


def test_artifacts_packets_and_direct_sources_account_without_reservations(store):
    h = seed_delivery(store)
    from simon.domain.native_delivery import manifest_bytes

    original = h.revision.size_bytes + h.source.size_bytes + len(h.source.text.encode("utf-8"))
    assert store.storage_usage(h.workspace)["retained_bytes"] == original
    packet = packet_record(h)
    amount = len(manifest_bytes(packet.manifest)) + sum(
        p.size_bytes for p in packet.manifest.previews
    )
    store.save_storage_entitlement(policy(h, original + amount), 0)
    store.insert_delivery_packet(packet)
    assert store.storage_charge(h.workspace, "delivery", packet.id).bytes == amount
    assert (
        store.storage_charge(h.workspace, "artifact", h.revision.id).bytes == h.revision.size_bytes
    )
    assert store.storage_usage(h.workspace)["used_bytes"] == original + amount
    with pytest.raises(StorageQuotaExceededError):
        store.insert_delivery_packet(packet_record(h))
    assert len(store.delivery_packets(h.workspace, h.first.id)) == 1


def test_invalid_source_snapshot_cannot_offset_charge_with_negative_size(storage):
    h = storage
    invalid = source(h, text="Valid extraction").model_copy(update={"size_bytes": -1})
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_intake_source(invalid)
    assert h.store.native_intake_source(h.workspace, h.first.id, invalid.id) is None
    assert h.store.storage_usage(h.workspace)["used_bytes"] == 0


def test_memory_source_write_failure_restores_only_its_previous_charge(monkeypatch):
    from simon.adapters.memory import InMemoryStore

    store = InMemoryStore()
    h = seed_native_projects(store)
    evidence = source(h)
    held = reservation(
        h, object_id=evidence.id, bytes=evidence.size_bytes + len(evidence.text.encode())
    )
    store.reserve_source_storage(held)
    retain = store._retain_storage

    def fail(value):
        retain(value)
        raise RuntimeError("Injected failure after charge write")

    monkeypatch.setattr(store, "_retain_storage", fail)
    with pytest.raises(RuntimeError, match="after charge write"):
        store.insert_native_intake_source(evidence)
    assert store.native_intake_source(h.workspace, h.first.id, evidence.id) is None
    assert store.storage_charge(h.workspace, "source", evidence.id) == held


def test_concurrent_reservations_and_entitlement_writers_have_one_winner(storage):
    h = storage
    h.store.save_storage_entitlement(policy(h, 10), 0)
    values = tuple(reservation(h, bytes=7) for _ in range(2))

    def reserve(value):
        try:
            h.store.reserve_source_storage(value)
            return True
        except StorageQuotaExceededError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(reserve, values)) == 1
    assert h.store.storage_usage(h.workspace)["used_bytes"] == 7

    def append(value):
        try:
            h.store.save_storage_entitlement(value, 1)
            return True
        except InvalidTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(append, (policy(h, 20, version=2), policy(h, 30, version=2)))) == 1
    assert len(h.store.storage_entitlement_history(h.workspace)) == 2


@pytest.mark.postgres
def test_postgres_storage_immutable_bindings_terminal_states_and_direct_object_quota(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = seed_native_projects(store)
        grant = policy(h, 32)
        store.save_storage_entitlement(grant, 0)
        value = reservation(h)
        store.reserve_source_storage(value)
        for sql, parameters in (
            (
                "UPDATE native_storage_entitlements SET snapshot=snapshot WHERE workspace_id=%s",
                (h.workspace,),
            ),
            ("DELETE FROM native_storage_entitlements WHERE workspace_id=%s", (h.workspace,)),
            ("DELETE FROM native_storage_charges WHERE object_id=%s", (value.object_id,)),
            (
                "UPDATE native_storage_charges SET bytes=0,"
                "snapshot=jsonb_set(snapshot,'{bytes}','0') WHERE object_id=%s",
                (value.object_id,),
            ),
            (
                "UPDATE native_storage_charges SET project_id=%s WHERE object_id=%s",
                (h.second.id, value.object_id),
            ),
        ):
            with pytest.raises(psycopg.IntegrityError), store.transaction():
                store.connection.execute(sql, parameters)
        evidence = source(h, size_bytes=0, text="x")
        with pytest.raises(StorageQuotaExceededError):
            store.insert_native_intake_source(evidence)
        assert store.native_intake_source(h.workspace, h.first.id, evidence.id) is None
        store.release_source_storage(h.workspace, value.object_id)
        store.insert_native_intake_source(evidence)
        for sql, parameters in (
            (
                "UPDATE native_storage_charges SET state='reserved',"
                "snapshot=jsonb_set(snapshot,'{state}','\"reserved\"') WHERE object_id=%s",
                (value.object_id,),
            ),
            (
                "UPDATE native_intake_sources SET "
                "snapshot=jsonb_set(snapshot,'{text}','\"larger content\"') WHERE id=%s",
                (evidence.id,),
            ),
            ("DELETE FROM native_intake_sources WHERE id=%s", (evidence.id,)),
        ):
            with pytest.raises(psycopg.IntegrityError), store.transaction():
                store.connection.execute(sql, parameters)
        assert store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
        assert store.storage_usage(h.workspace)["used_bytes"] == 1
    finally:
        store.close()
