"""Immutable scoped package evidence is retained and admitted atomically."""

from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError, StorageQuotaExceededError
from simon.domain.native_dependencies import canonical_bytes
from simon.services.native_acceptance import NativeAcceptanceService
from tests.unit.test_native_acceptance import accept, assign, setup_package, validate
from tests.unit.test_storage_workflows import limit


@pytest.fixture
def package(store, monkeypatch):
    monkeypatch.setattr("tests.unit.test_project_models.InMemoryStore", lambda: store)
    return setup_package()


def saved_records(h):
    value, _ = validate(h, assign(h))
    accept(h, value)
    return (
        h.store.delivery_profile_assignment(h.workspace, h.first.id, h.packet_id),
        h.store.delivery_validations(h.workspace, h.first.id, h.packet_id)[0],
        h.store.delivery_acceptances(h.workspace, h.first.id, h.packet_id)[0],
    )


def test_package_records_are_immutable_scoped_owned_and_metered(package):
    h = package
    records = saved_records(h)
    assert h.store.delivery_profile_assignments(h.workspace, h.first.id, h.packet_id) == (
        records[0],
    )
    for record, method, kind in (
        (
            records[0],
            lambda value: h.store.insert_delivery_profile_assignment(value, 0),
            "delivery_validation",
        ),
        (records[1], h.store.insert_delivery_validation, "delivery_validation"),
        (records[2], h.store.insert_delivery_acceptance, "acceptance"),
    ):
        charge = h.store.storage_charge(h.workspace, kind, record.id)
        assert charge.state == "retained" and charge.bytes == len(canonical_bytes(record))
        with pytest.raises(InvalidTransitionError):
            method(record)
        for changes in (
            {"id": uuid4(), "project_id": h.second.id},
            {"id": uuid4(), "workspace_id": h.foreign_workspace},
            {"id": uuid4(), "created_by": h.member},
        ):
            with pytest.raises(InvalidTransitionError):
                method(record.model_copy(update=changes))
    assert h.store.delivery_validation(h.workspace, h.second.id, records[1].id) is None
    assert h.store.delivery_acceptance(h.foreign_workspace, h.first.id, records[2].id) is None
    assert h.store.delivery_profile_assignments(h.workspace, h.second.id, h.packet_id) == ()
    records[1].evidence["probe"]["published"] = True
    assert not h.store.delivery_validation(h.workspace, h.first.id, records[1].id).evidence[
        "probe"
    ]["published"]


def test_acceptance_record_quota_failure_rolls_back_history_receipt_and_charge(package):
    h = package
    value, _ = validate(h, assign(h))
    before = h.store.storage_usage(h.workspace)
    limit(h, before["used_bytes"])
    with pytest.raises(StorageQuotaExceededError):
        accept(h, value)
    assert h.store.delivery_acceptances(h.workspace, h.first.id, h.packet_id) == ()
    assert h.store.storage_usage(h.workspace) == before
    assert not h.acceptance.status(h.owner_actor, h.first.id, h.packet_id)["applicability"][
        "accepted"
    ]


@pytest.mark.postgres
def test_package_sql_immutability_current_assignment_scope_and_restart(postgres_url, monkeypatch):
    import psycopg
    from psycopg import sql

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=4)
    restarted = None
    try:
        monkeypatch.setattr("tests.unit.test_project_models.InMemoryStore", lambda: store)
        h = setup_package()
        records = saved_records(h)
        for table, record in zip(
            (
                "native_delivery_profile_assignments",
                "native_delivery_validations",
                "native_delivery_acceptances",
            ),
            records,
            strict=True,
        ):
            for statement in (
                "UPDATE {} SET snapshot=snapshot WHERE id=%s",
                "DELETE FROM {} WHERE id=%s",
            ):
                with (
                    pytest.raises(psycopg.IntegrityError),
                    psycopg.connect(postgres_url) as connection,
                ):
                    connection.execute(
                        sql.SQL(statement).format(sql.Identifier(table)), (record.id,)
                    )
        forged = records[1].model_copy(update={"id": uuid4(), "project_id": h.second.id})
        with pytest.raises(InvalidTransitionError):
            store._artifact_write("native_delivery_validations", forged, None)
        restarted = PostgresStore(postgres_url, pool_size=4)
        h.acceptance = NativeAcceptanceService(restarted, h.delivery)
        status = h.acceptance.status(h.owner_actor, h.first.id, h.packet_id)
        assert status["applicability"]["accepted"]
        assert status["latest_acceptance"]["id"] == str(records[2].id)
        assert restarted.storage_usage(h.workspace) == store.storage_usage(h.workspace)
    finally:
        if restarted is not None:
            restarted.close()
        store.close()
