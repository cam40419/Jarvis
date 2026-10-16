"""Continuity histories bind scoped evidence and retained bytes atomically."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError, StorageQuotaExceededError
from simon.domain.native_artifacts import NativeArtifact
from simon.domain.native_continuity import (
    NativeContinuityHandoff,
    NativeContinuityManifest,
    continuity_manifest_bytes,
)
from simon.domain.native_projects import NativeProjectMember
from simon.domain.native_storage import StorageEntitlement
from tests.contract.test_native_artifact_store import revision_record
from tests.contract.test_native_execution_store import run_record, seed_execution
from tests.contract.test_native_intake_store import source


@pytest.fixture
def handoff(store):
    h = seed_execution(store)
    h.artifact = NativeArtifact(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        name="Editable work",
        created_by_agent_id=h.agent.id,
    )
    store.insert_artifact(h.artifact)
    h.revision = revision_record(h)
    store.insert_artifact_revision(h.revision)
    h.run = run_record(h)
    store.insert_execution_run(h.run)
    h.source = source(h)
    store.insert_native_intake_source(h.source)
    store.put_native_project_member(
        NativeProjectMember(
            workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member, role="member"
        )
    )
    h.manifest = NativeContinuityManifest(
        title="Supported handoff",
        project_version=h.first.version,
        source_revision_ids=(h.source.id,),
        artifacts=(
            {
                "artifact_id": h.artifact.id,
                "revision_id": h.revision.id,
                "sha256": h.revision.sha256,
            },
        ),
        runs=(
            {
                "run_id": h.run.id,
                "version": h.run.version,
                "step_number": h.run.step_number,
                "applied_step": h.run.applied_step,
            },
        ),
        snapshot={
            "accepted": [],
            "candidates": [str(h.artifact.id)],
            "costs": {"known": 0, "held": 0},
        },
    )
    return h


def record(h, **changes):
    return NativeContinuityHandoff(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "created_by": h.owner,
            "recipient_id": h.member,
            "manifest": h.manifest,
            "manifest_sha256": hashlib.sha256(continuity_manifest_bytes(h.manifest)).hexdigest(),
            **changes,
        }
    )


def test_handoff_scoped_history_exact_refs_immutable_copy_and_retained_charge(handoff):
    h = handoff
    value = record(h)
    h.store.insert_continuity_handoff(value)
    assert h.store.continuity_handoff(h.workspace, h.first.id, value.id) == value
    assert h.store.continuity_handoffs(h.workspace, h.first.id) == (value,)
    assert h.store.continuity_handoffs(h.workspace, h.first.id, 1, 1) == ()
    charge = h.store.storage_charge(h.workspace, "continuity", value.id)
    assert charge.bytes == len(continuity_manifest_bytes(h.manifest)) and charge.state == "retained"
    value.manifest.snapshot["accepted"].append("caller mutation")
    assert (
        h.store.continuity_handoff(h.workspace, h.first.id, value.id).manifest.snapshot["accepted"]
        == []
    )
    for ws, pid in ((h.workspace, h.second.id), (h.foreign_workspace, h.first.id)):
        assert h.store.continuity_handoff(ws, pid, value.id) is None
        assert h.store.continuity_handoffs(ws, pid) == ()
    with pytest.raises(InvalidTransitionError):
        h.store.insert_continuity_handoff(value)


def test_handoff_cross_project_recipient_and_checkpoint_forgery_are_rejected(handoff):
    h = handoff
    for changes in (
        {"project_id": h.second.id},
        {"recipient_id": h.outsider},
        {"created_by": h.outsider},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_continuity_handoff(record(h, **changes))
    changed = h.manifest.model_copy(
        update={"runs": (h.manifest.runs[0].model_copy(update={"version": 99}),)}
    )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_continuity_handoff(
            record(
                h,
                manifest=changed,
                manifest_sha256=hashlib.sha256(continuity_manifest_bytes(changed)).hexdigest(),
            )
        )


def test_competing_handoffs_reserve_one_allowance_without_double_charging(handoff):
    h = handoff
    usage = h.store.storage_usage(h.workspace)["used_bytes"]
    size = len(continuity_manifest_bytes(h.manifest))
    h.store.save_storage_entitlement(
        StorageEntitlement(
            workspace_id=h.workspace,
            version=1,
            plan_key="bounded",
            limit_bytes=usage + size,
            issued_by=h.owner,
            reason="One handoff allowance",
        ),
        0,
    )
    candidates = (record(h), record(h))

    def insert(value):
        try:
            h.store.insert_continuity_handoff(value)
            return value
        except StorageQuotaExceededError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(insert, candidates))
    assert sum(r is not None for r in results) == 1
    assert h.store.storage_usage(h.workspace)["used_bytes"] == usage + size
    assert len(h.store.continuity_handoffs(h.workspace, h.first.id)) == 1


@pytest.mark.postgres
def test_handoff_direct_sql_immutability_and_manifest_hash_rejected(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=4)
    try:
        h = handoff.__wrapped__(store)
        value = record(h)
        store.insert_continuity_handoff(value)
        for statement in (
            "UPDATE native_continuity_handoffs SET snapshot=snapshot WHERE id=%s",
            "DELETE FROM native_continuity_handoffs WHERE id=%s",
        ):
            with pytest.raises(psycopg.IntegrityError), psycopg.connect(postgres_url) as connection:
                connection.execute(statement, (value.id,))
        forged = value.model_copy(update={"id": uuid4(), "manifest_sha256": "f" * 64})
        with pytest.raises(InvalidTransitionError):
            store._artifact_write("native_continuity_handoffs", forged, None)
    finally:
        store.close()
