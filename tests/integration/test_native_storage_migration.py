"""Storage migration accounts existing data without retroactive admission or history loss."""

import psycopg
import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.errors import StorageQuotaExceededError
from simon.domain.native_delivery import manifest_bytes
from simon.domain.native_storage import DEFAULT_STORAGE_LIMIT_BYTES
from simon.migrate import migrate, migration_directory
from tests.contract.test_native_delivery_store import packet_record, seed_delivery
from tests.contract.test_native_intake_store import source
from tests.integration.test_native_project_migration import (
    empty_native_schema as empty_native_schema,
)
from tests.integration.test_native_project_migration import table_rows

pytestmark = pytest.mark.postgres
STORAGE_MIGRATION = "0037_native_storage_entitlements.sql"


@pytest.fixture
def storage_migrations(tmp_path):
    predecessor, target = tmp_path / "through_0036", tmp_path / "through_0037"
    predecessor.mkdir()
    target.mkdir()
    for path in migration_directory().glob("*.sql"):
        if path.name.endswith(".down.sql") or path.name > STORAGE_MIGRATION:
            continue
        (target / path.name).write_bytes(path.read_bytes())
        if path.name < STORAGE_MIGRATION:
            (predecessor / path.name).write_bytes(path.read_bytes())
    return predecessor, target


def test_storage_backfill_preserves_overquota_sources_artifacts_and_delivery(
    empty_native_schema,
    storage_migrations,
):
    predecessor, target = storage_migrations
    migrate(empty_native_schema, predecessor)
    store = PostgresStore(empty_native_schema, pool_size=2)
    try:
        h = seed_delivery(store)
        packet = packet_record(h)
        store.insert_delivery_packet(packet)
        expected = h.revision.size_bytes + h.source.size_bytes + len(h.source.text.encode())
        expected += len(manifest_bytes(packet.manifest)) + sum(
            p.size_bytes for p in packet.manifest.previews
        )
        for index in range(55):
            evidence = source(
                h, source_key=f"Historical-{index}.txt", size_bytes=5 * 1024 * 1024, text="é"
            )
            store.insert_native_intake_source(evidence)
            expected += evidence.size_bytes + len(evidence.text.encode())
        assert expected > DEFAULT_STORAGE_LIMIT_BYTES
        tables = ("native_intake_sources", "native_artifact_revisions", "native_delivery_packets")
        before = table_rows(empty_native_schema, tables)
        assert migrate(empty_native_schema, target) == [STORAGE_MIGRATION]
        assert table_rows(empty_native_schema, tables) == before
        assert store.storage_entitlement(h.workspace) is None
        assert store.storage_usage(h.workspace) == {
            "retained_bytes": expected,
            "reserved_bytes": 0,
            "used_bytes": expected,
            "reservation_count": 0,
        }
        assert len(store.storage_charges(h.workspace)) == 58
        assert all(value.state == "retained" for value in store.storage_charges(h.workspace))
        with pytest.raises(StorageQuotaExceededError):
            store.insert_native_intake_source(
                source(h, source_key="New-positive.txt", size_bytes=0, text="x")
            )
        store.insert_native_intake_source(
            source(h, source_key="New-zero.txt", size_bytes=0, text="")
        )
        assert store.storage_usage(h.workspace)["used_bytes"] == expected
        assert migrate(empty_native_schema, target) == []
    finally:
        store.close()


def test_storage_migration_failure_rolls_back_new_objects_and_retries(
    empty_native_schema,
    storage_migrations,
):
    predecessor, target = storage_migrations
    migrate(empty_native_schema, predecessor)
    before = table_rows(empty_native_schema, ("schema_migrations",))
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute("CREATE TABLE native_storage_charges (sentinel text PRIMARY KEY)")
        connection.execute("INSERT INTO native_storage_charges VALUES ('preserve')")
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(empty_native_schema, target)
    assert table_rows(empty_native_schema, ("schema_migrations",)) == before
    with psycopg.connect(empty_native_schema) as connection:
        assert (
            connection.execute("SELECT to_regclass('native_storage_entitlements')").fetchone()[0]
            is None
        )
        assert connection.execute("SELECT sentinel FROM native_storage_charges").fetchall() == [
            ("preserve",)
        ]
        connection.execute("DROP TABLE native_storage_charges")
    assert migrate(empty_native_schema, target) == [STORAGE_MIGRATION]
