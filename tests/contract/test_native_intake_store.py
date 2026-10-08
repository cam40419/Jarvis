"""Durable intake context, immutable evidence revisions and planning reservations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_intake import (
    IntakeRun,
    IntakeSource,
    NativeIntake,
    ProposalReview,
    StaffingProposal,
)
from tests.contract.test_native_project_store import seed_native_projects


@pytest.fixture
def intake(store):
    h = seed_native_projects(store)
    h.intake = NativeIntake(
        workspace_id=h.workspace, project_id=h.first.id, version=1, answers={"audience": "Artists"}
    )
    store.save_native_intake(h.intake, 0)
    return h


def source(h, **changes):
    values = {
        "workspace_id": h.workspace,
        "project_id": h.first.id,
        "source_key": "Brand/Direction.txt",
        "filename": "Direction.txt",
        "media_type": "text/plain",
        "revision": 1,
        "sha256": "a" * 64,
        "size_bytes": 10,
        "text": "Our design direction.",
        "extraction_status": "text",
        "created_by": h.owner,
    }
    return IntakeSource(**(values | changes))


def run(h, **changes):
    now = utc_now()
    values = {
        "workspace_id": h.workspace,
        "project_id": h.first.id,
        "requested_by": h.owner,
        "idempotency_key": "initial-analysis",
        "request_digest": "b" * 64,
        "snapshot_digest": "c" * 64,
        "intake_version": h.intake.version,
        "endpoint_id": "configured-local",
        "model": "model-name",
        "reserved_microusd": 200_000,
        "started_at": now,
        "deadline_at": now + timedelta(minutes=3),
    }
    return IntakeRun(**(values | changes))


def test_intake_compare_and_swap_is_project_scoped_and_does_not_alias_answers(intake):
    h = intake
    assert h.store.native_intake(h.workspace, h.first.id) == h.intake
    assert h.store.native_intake(h.foreign_workspace, h.first.id) is None
    assert h.store.native_intake(h.workspace, h.second.id) is None
    h.intake.answers["audience"] = "Caller mutation"
    read = h.store.native_intake(h.workspace, h.first.id)
    assert read.answers["audience"] == "Artists"
    read.answers["audience"] = "Read mutation"
    assert h.store.native_intake(h.workspace, h.first.id).answers["audience"] == "Artists"
    for value, expected in (
        (h.intake, 0),
        (h.intake.model_copy(update={"project_id": h.foreign.id}), 0),
        (h.intake.model_copy(update={"version": 3}), 1),
        (h.intake.model_copy(update={"project_id": h.second.id, "version": 2}), 1),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.save_native_intake(value, expected)

    def save(outcome):
        value = h.intake.model_copy(update={"version": 2, "outcomes": outcome})
        try:
            h.store.save_native_intake(value, 1)
        except InvalidTransitionError:
            return None
        return value

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [
            value for value in executor.map(save, ("First outcome", "Second outcome")) if value
        ]
    assert len(winners) == 1
    assert h.store.native_intake(h.workspace, h.first.id) == winners[0]


def test_sources_keep_revisions_and_revocations_without_cross_project_reads(intake):
    h = intake
    original = source(h)
    replacement = source(h, revision=2, created_at=original.created_at + timedelta(seconds=1))
    other = source(h, project_id=h.second.id)
    for value in (original, replacement, other):
        h.store.insert_native_intake_source(value)
    assert h.store.native_intake_sources(h.workspace, h.first.id) == (original, replacement)
    assert h.store.native_intake_sources(h.workspace, h.second.id) == (other,)
    assert h.store.native_intake_sources(h.foreign_workspace, h.first.id) == ()
    assert h.store.native_intake_source(h.workspace, h.first.id, original.id) == original
    assert h.store.native_intake_source(h.foreign_workspace, h.first.id, original.id) is None
    assert h.store.native_intake_source(h.workspace, h.second.id, original.id) is None
    for value in (
        original,
        original.model_copy(update={"id": uuid4()}),
        source(h, source_key="Foreign.txt", project_id=h.foreign.id),
        source(h, source_key="Unknown.txt", created_by=uuid4()),
        source(h, source_key="Foreign-user.txt", created_by=h.outsider),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_native_intake_source(value)
    at = replacement.created_at + timedelta(seconds=1)
    assert not h.store.revoke_native_intake_source(
        h.foreign_workspace, h.first.id, replacement.id, at
    )
    assert not h.store.revoke_native_intake_source(h.workspace, h.second.id, replacement.id, at)
    with pytest.raises(InvalidTransitionError):
        h.store.revoke_native_intake_source(
            h.workspace, h.first.id, replacement.id, original.created_at
        )
    assert h.store.revoke_native_intake_source(h.workspace, h.first.id, replacement.id, at)
    assert not h.store.revoke_native_intake_source(h.workspace, h.first.id, replacement.id, at)
    saved = h.store.native_intake_sources(h.workspace, h.first.id)
    assert saved[0] == original
    assert saved[1] == replacement.model_copy(update={"revoked_at": at})
    assert max(saved, key=lambda value: value.revision).revoked_at == at


def test_runs_require_current_intake_and_human_requester_and_unique_attempt_key(intake):
    h = intake
    value = run(h)
    assert h.store.native_intake_runs(h.workspace, h.first.id) == ()
    assert h.store.native_intake_run_by_key(h.workspace, h.first.id, value.idempotency_key) is None
    assert h.store.native_intake_run(h.workspace, h.first.id, value.id) is None
    h.store.insert_native_intake_run(value)
    assert h.store.native_intake_run(h.workspace, h.first.id, value.id) == value
    assert h.store.native_intake_run(h.workspace, h.second.id, value.id) is None
    assert h.store.native_intake_run(h.foreign_workspace, h.first.id, value.id) is None
    assert h.store.native_intake_run_by_key(h.workspace, h.first.id, value.idempotency_key) == value
    assert h.store.native_intake_run_by_key(h.workspace, h.second.id, value.idempotency_key) is None
    assert h.store.native_intake_runs(h.foreign_workspace, h.first.id) == ()
    for invalid in (
        value,
        value.model_copy(update={"id": uuid4()}),
        run(h, idempotency_key="new-attempt", version=2),
        run(h, idempotency_key="new-attempt", intake_version=2),
        run(h, idempotency_key="new-attempt", project_id=h.second.id),
        run(h, idempotency_key="new-attempt", requested_by=uuid4()),
        run(h, idempotency_key="new-attempt", requested_by=h.outsider),
        run(h, idempotency_key="new-attempt", project_id=h.foreign.id),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_native_intake_run(invalid)
    newer = run(h, idempotency_key="second-attempt")
    h.store.insert_native_intake_run(newer)
    assert h.store.native_intake_runs(h.workspace, h.first.id) == (newer, value)


def test_run_compare_and_swap_preserves_dispatch_context_and_settles_reservation(intake):
    h = intake
    original = run(h)
    h.store.insert_native_intake_run(original)
    for changes in (
        {"id": uuid4()},
        {"project_id": h.second.id},
        {"workspace_id": h.foreign_workspace},
        {"requested_by": h.member},
        {"idempotency_key": "new-attempt"},
        {"request_digest": "d" * 64},
        {"snapshot_digest": "d" * 64},
        {"intake_version": 2},
        {"endpoint_id": "another-endpoint"},
        {"model": "another-model"},
        {"included_source_ids": (uuid4(),)},
        {"omitted_source_ids": (uuid4(),)},
        {"started_at": original.started_at + timedelta(seconds=1)},
        {"deadline_at": original.deadline_at + timedelta(minutes=1)},
        {"version": 3},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_native_intake_run(
                original.model_copy(update={"version": 2, **changes}), 1
            )
    proposal = StaffingProposal(summary="Use existing roles", next_milestone="Review evidence")
    ready = original.model_copy(
        update={
            "version": 2,
            "status": "ready",
            "proposal": proposal,
            "review": ProposalReview(approved=True),
            "reserved_microusd": 0,
            "charged_microusd": 35_100,
            "input_tokens": 1000,
            "output_tokens": 100,
            "finished_at": utc_now(),
        }
    )
    h.store.update_native_intake_run(ready, 1)
    assert h.store.native_intake_run(h.workspace, h.first.id, original.id) == ready
    assert (
        h.store.native_intake_run_by_key(h.workspace, h.first.id, original.idempotency_key) == ready
    )
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_intake_run(ready, 1)
    # Historical attempts remain readable after source/context edits change the intake version.
    h.store.save_native_intake(h.intake.model_copy(update={"version": 2}), 1)
    assert h.store.native_intake_run(h.workspace, h.first.id, original.id) == ready


def test_racing_run_settlement_has_one_winner(intake):
    h = intake
    original = run(h)
    h.store.insert_native_intake_run(original)

    def save(status):
        changed = original.model_copy(update={"version": 2, "status": status})
        try:
            h.store.update_native_intake_run(changed, 1)
        except InvalidTransitionError:
            return None
        return changed

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(save, ("unknown", "cancelled")) if value]
    assert len(winners) == 1
    assert h.store.native_intake_run(h.workspace, h.first.id, original.id) == winners[0]
    assert winners[0].reserved_microusd == original.reserved_microusd


def test_intake_source_and_attempt_changes_roll_back_as_one_transaction(intake):
    h = intake
    original = source(h)
    attempt = run(h)
    h.store.insert_native_intake_source(original)
    h.store.insert_native_intake_run(attempt)
    replacement = source(h, revision=2)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.save_native_intake(h.intake.model_copy(update={"version": 2}), 1)
        h.store.insert_native_intake_source(replacement)
        h.store.revoke_native_intake_source(h.workspace, h.first.id, original.id, utc_now())
        h.store.update_native_intake_run(
            attempt.model_copy(update={"version": 2, "status": "stale"}), 1
        )
        h.store.insert_native_intake_run(run(h, intake_version=2, idempotency_key="new-attempt"))
        raise RuntimeError("No partial evidence, reservation, or context commits")
    assert h.store.native_intake(h.workspace, h.first.id) == h.intake
    assert h.store.native_intake_sources(h.workspace, h.first.id) == (original,)
    assert h.store.native_intake_runs(h.workspace, h.first.id) == (attempt,)


@pytest.mark.postgres
def test_database_intake_rejects_cross_project_identity_and_snapshot_drift(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = seed_native_projects(store)
        h.intake = NativeIntake(workspace_id=h.workspace, project_id=h.first.id, version=1)
        store.save_native_intake(h.intake, 0)
        evidence, attempt = source(h), run(h)
        store.insert_native_intake_source(evidence)
        store.insert_native_intake_run(attempt)
        for statement, params in (
            ("UPDATE native_intakes SET version=2 WHERE project_id=%s", (h.first.id,)),
            ("UPDATE native_intake_sources SET revision=2 WHERE id=%s", (evidence.id,)),
            ("UPDATE native_intake_sources SET revoked_at=now() WHERE id=%s", (evidence.id,)),
            ("UPDATE native_intake_runs SET reserved_microusd=0 WHERE id=%s", (attempt.id,)),
            ("UPDATE native_intake_runs SET status='failed' WHERE id=%s", (attempt.id,)),
            ("UPDATE native_intake_runs SET version=2 WHERE id=%s", (attempt.id,)),
        ):
            with pytest.raises(psycopg.errors.CheckViolation), store.transaction():
                store.connection.execute(statement, params)
        # Change the column and snapshot together to isolate the composite FK constraint.
        with pytest.raises(psycopg.errors.ForeignKeyViolation), store.transaction():
            store.connection.execute(
                "UPDATE native_intake_sources SET workspace_id=%s,"
                "snapshot=jsonb_set(snapshot,'{workspace_id}',to_jsonb(%s::text)) WHERE id=%s",
                (h.foreign_workspace, str(h.foreign_workspace), evidence.id),
            )
        store.close()
        assert store.native_intake(h.workspace, h.first.id) == h.intake
        assert store.native_intake_source(h.workspace, h.first.id, evidence.id) == evidence
        assert store.native_intake_run(h.workspace, h.first.id, attempt.id) == attempt
    finally:
        store.close()
