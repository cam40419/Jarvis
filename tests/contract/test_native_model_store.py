"""Shared model credentials, policies and usage preserve scope and settled liabilities."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.model_routing import ModelEndpoint
from simon.domain.native_models import (
    ModelResourcePolicy,
    ModelUsage,
    ProjectModel,
    ProjectModelCredential,
    UsageTotals,
)
from tests.contract.test_native_project_store import seed_native_projects


@pytest.fixture
def models(store):
    h = seed_native_projects(store)
    h.model = ProjectModel(
        workspace_id=h.workspace,
        project_id=h.first.id,
        template_id="local",
        label="Local",
        created_by=h.owner,
    )
    store.insert_project_model(h.model)
    return h


def usage(h, **changes):
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    endpoint = ModelEndpoint(
        id="local",
        model="weights",
        provider="openai_compatible",
        base_url="http://localhost:1234/v1",
        local=True,
    )
    return ModelUsage(
        **(
            {
                "workspace_id": h.workspace,
                "project_id": h.first.id,
                "operation_id": uuid4(),
                "phase": "generation",
                "requested_by": h.owner,
                "model_id": h.model.id,
                "model_version": 1,
                "template_id": "local",
                "model": "weights",
                "endpoint_fingerprint": "a" * 64,
                "endpoint_snapshot": endpoint.model_dump(mode="json"),
                "input_rate": Decimal("0.01"),
                "output_rate": Decimal("0.04"),
                "reserved_microusd": 100,
                "held_microusd": 100,
                "started_at": now,
                "deadline_at": now + timedelta(minutes=3),
            }
            | changes
        )
    )


def test_model_enrollment_is_scoped_and_mutations_preserve_identity(models):
    h = models
    assert h.store.project_models(h.workspace, h.first.id) == (h.model,)
    assert h.store.project_models(h.workspace, h.second.id) == ()
    assert h.store.project_model(h.foreign_workspace, h.first.id, h.model.id) is None
    assert h.store.project_model(h.workspace, h.second.id, h.model.id) is None
    for changes in (
        {},
        {"id": uuid4(), "project_id": h.foreign.id},
        {"id": uuid4(), "created_by": h.outsider},
        {"id": uuid4(), "version": 2},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_project_model(h.model.model_copy(update=changes))
    for changes in (
        {"id": uuid4()},
        {"project_id": h.second.id},
        {"template_id": "other"},
        {"created_by": h.member},
        {"version": 3},
        {"qualification_usage_id": uuid4()},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_project_model(h.model.model_copy(update={"version": 2, **changes}), 1)
    revised = h.model.model_copy(update={"version": 2, "label": "Revised", "enabled": False})
    h.store.update_project_model(revised, 1)
    assert h.store.project_models(h.workspace, h.first.id) == (revised,)
    with pytest.raises(InvalidTransitionError):
        h.store.update_project_model(revised, 1)


def test_ciphertext_revisions_are_immutable_and_project_bound(models):
    h = models
    credential = ProjectModelCredential(
        workspace_id=h.workspace,
        project_id=h.first.id,
        model_id=h.model.id,
        revision=1,
        encrypted_secret="ciphertext-not-plaintext",
        created_by=h.owner,
    )
    assert h.store.project_model_credential(h.workspace, h.first.id, h.model.id, 1) is None
    h.store.insert_project_model_credential(credential)
    assert h.store.project_model_credential(h.workspace, h.first.id, h.model.id, 1) == credential
    assert h.store.project_model_credential(h.workspace, h.first.id, h.model.id, 2) is None
    assert h.store.project_model_credential(h.workspace, h.second.id, h.model.id, 1) is None
    assert h.store.project_model_credential(h.foreign_workspace, h.first.id, h.model.id, 1) is None
    for changes in (
        {},
        {"id": uuid4()},
        {"id": uuid4(), "project_id": h.second.id},
        {"id": uuid4(), "revision": 2, "created_by": h.outsider},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_project_model_credential(credential.model_copy(update=changes))
    second = credential.model_copy(
        update={"id": uuid4(), "revision": 2, "encrypted_secret": "new-ciphertext"}
    )
    h.store.insert_project_model_credential(second)
    revised = h.model.model_copy(update={"version": 2, "credential_revision": 2})
    h.store.update_project_model(revised, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_project_model(
            revised.model_copy(update={"version": 3, "credential_revision": 1}), 2
        )
    assert h.store.project_model_credential(h.workspace, h.first.id, h.model.id, 1) == credential


def test_workspace_and_project_policies_have_independent_cas_and_model_scope(models):
    h = models
    workspace = ModelResourcePolicy(
        workspace_id=h.workspace, version=1, lifetime_limit_microusd=1000
    )
    project = ModelResourcePolicy(
        workspace_id=h.workspace, project_id=h.first.id, version=1, planning_model_id=h.model.id
    )
    assert h.store.model_resource_policy(h.workspace) is None
    for value in (workspace, project):
        h.store.save_model_resource_policy(value, 0)
        assert h.store.model_resource_policy(value.workspace_id, value.project_id) == value
        with pytest.raises(InvalidTransitionError):
            h.store.save_model_resource_policy(value, 0)
    for value in (
        project.model_copy(update={"project_id": h.second.id}),
        project.model_copy(update={"project_id": h.foreign.id}),
        workspace.model_copy(update={"workspace_id": uuid4()}),
        project.model_copy(update={"version": 3}),
        project.model_copy(update={"version": 2, "review_model_id": uuid4()}),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.save_model_resource_policy(value, 1 if value.version > 1 else 0)

    def save(limit):
        updated = project.model_copy(update={"version": 2, "daily_limit_microusd": limit})
        try:
            h.store.save_model_resource_policy(updated, 1)
        except InvalidTransitionError:
            return None
        return updated

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(save, (100, 200)) if value]
    assert len(winners) == 1
    assert h.store.model_resource_policy(h.workspace, h.first.id) == winners[0]
    newer = workspace.model_copy(update={"version": 2, "paused": True})
    h.store.save_model_resource_policy(newer, 1)
    assert h.store.model_resource_policy(h.workspace) == newer


def test_usage_insertion_requires_scope_model_revision_requester_and_unique_phase(models):
    h = models
    value = usage(h)
    assert h.store.model_usage(h.workspace, h.first.id, value.id) is None
    h.store.insert_model_usage(value)
    assert h.store.model_usage(h.workspace, h.first.id, value.id) == value
    assert h.store.model_usage(h.workspace, h.second.id, value.id) is None
    assert h.store.model_usage(h.foreign_workspace, h.first.id, value.id) is None
    assert h.store.model_usage_for_operation(h.workspace, h.first.id, value.operation_id) == (
        value,
    )
    assert h.store.model_usage_for_operation(h.workspace, h.second.id, value.operation_id) == ()
    for changes in (
        {},
        {"id": uuid4()},
        {"id": uuid4(), "version": 2},
        {"id": uuid4(), "phase": "review", "model_version": 2},
        {"id": uuid4(), "phase": "review", "model_id": uuid4()},
        {"id": uuid4(), "phase": "review", "credential_revision": 1},
        {"id": uuid4(), "phase": "review", "requested_by": h.outsider},
        {"id": uuid4(), "phase": "review", "project_id": h.second.id},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_model_usage(value.model_copy(update=changes))
    reviewer = value.model_copy(update={"id": uuid4(), "phase": "review"})
    h.store.insert_model_usage(reviewer)
    assert h.store.model_usage_for_operation(h.workspace, h.first.id, value.operation_id) == (
        value,
        reviewer,
    )
    credential = ProjectModelCredential(
        workspace_id=h.workspace,
        project_id=h.first.id,
        model_id=h.model.id,
        revision=1,
        encrypted_secret="ciphertext",
        created_by=h.owner,
    )
    h.store.insert_project_model_credential(credential)
    keyed = usage(h, credential_revision=1)
    h.store.insert_model_usage(keyed)
    assert h.store.model_usage(h.workspace, h.first.id, keyed.id) == keyed


def test_usage_context_cannot_mutate_and_completed_accounting_is_immutable(models):
    h = models
    original = usage(h)
    h.store.insert_model_usage(original)
    original.endpoint_snapshot["model"] = "caller-mutated"
    current = h.store.model_usage(h.workspace, h.first.id, original.id)
    assert current.endpoint_snapshot["model"] == "weights"
    current.endpoint_snapshot["model"] = "read-mutated"
    value = h.store.model_usage(h.workspace, h.first.id, original.id)
    assert value.endpoint_snapshot["model"] == "weights"
    for changes in (
        {"operation_id": uuid4()},
        {"phase": "review"},
        {"requested_by": h.member},
        {"model_version": 2},
        {"model": "other"},
        {"reserved_microusd": 200},
        {"started_at": value.started_at + timedelta(seconds=1)},
        {"input_rate": Decimal(1)},
        {"endpoint_snapshot": {}},
        {"version": 3},
        {"id": uuid4()},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_model_usage(value.model_copy(update={"version": 2, **changes}), 1)
    changed = value.model_copy(update={"version": 2, "status": "dispatched"})
    h.store.update_model_usage(changed, 1)
    settled = changed.model_copy(
        update={
            "version": 3,
            "status": "settled",
            "held_microusd": 0,
            "charged_microusd": 125,
            "input_tokens": 100,
            "output_tokens": 10,
            "reported_model": "provider-dated-version",
        }
    )
    h.store.update_model_usage(settled, 2)
    assert h.store.model_usage(h.workspace, h.first.id, value.id) == settled
    for candidate, expected in (
        (settled, 2),
        (settled.model_copy(update={"version": 4, "charged_microusd": 0}), 3),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_model_usage(candidate, expected)


def test_unknown_reconciliation_races_preserve_one_final_evidence_record(models):
    h = models
    value = usage(h, status="unknown")
    h.store.insert_model_usage(value)

    def save(amount):
        reconciled = ModelUsage.model_validate(
            value.model_dump()
            | {
                "version": 2,
                "status": "reconciled",
                "held_microusd": 0,
                "charged_microusd": amount,
                "reconciliation_by": h.owner,
                "reconciliation_at": value.deadline_at,
                "reconciliation_reason": "Provider invoice checked",
                "reconciliation_evidence": f"Invoice total {amount} microUSD",
            }
        )
        try:
            h.store.update_model_usage(reconciled, 1)
        except InvalidTransitionError:
            return None
        return reconciled

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(save, (25, 35)) if value]
    assert len(winners) == 1
    assert h.store.model_usage(h.workspace, h.first.id, value.id) == winners[0]
    with pytest.raises(InvalidTransitionError):
        h.store.update_model_usage(
            winners[0].model_copy(update={"version": 3, "charged_microusd": 0}), 2
        )


def test_usage_totals_cross_project_ceiling_and_utc_period_rollover_keep_unknown_holds(models):
    h = models
    other = h.model.model_copy(update={"id": uuid4(), "project_id": h.second.id})
    h.store.insert_project_model(other)
    values = (
        usage(
            h,
            started_at=datetime(2026, 9, 30, 23, tzinfo=UTC),
            status="unknown",
            charged_microusd=15,
        ),
        usage(
            h,
            started_at=datetime(2026, 10, 1, 12, tzinfo=UTC),
            status="settled",
            held_microusd=0,
            charged_microusd=20,
        ),
        usage(h, status="settled", held_microusd=0, charged_microusd=30),
        usage(h, project_id=h.second.id, model_id=other.id, held_microusd=70),
        usage(h, status="dispatched"),
    )
    for value in values:
        h.store.insert_model_usage(value)
    at = datetime(2026, 10, 8, 8, tzinfo=timezone(timedelta(hours=-4)))
    assert h.store.model_usage_totals(h.workspace, None, at) == UsageTotals(
        charged_lifetime=65, charged_day=30, charged_month=50, held_microusd=270, active_calls=3
    )
    assert h.store.model_usage_totals(h.workspace, h.first.id, at).held_microusd == 200
    assert h.store.model_usage_totals(h.foreign_workspace, None, at) == UsageTotals()
    assert h.store.model_usage_totals(h.workspace, None, at.replace(month=11)).held_microusd == 270
    assert h.store.model_usage_totals(h.workspace, None, at.replace(month=11)).charged_month == 0
    expected = tuple(sorted(values, key=lambda item: (item.started_at, item.id), reverse=True))
    assert h.store.model_usage_entries(h.workspace, None, 1, 2) == expected[1:3]
    assert h.store.model_usage_entries(h.foreign_workspace) == ()
    assert len(h.store.model_usage_entries(h.workspace, h.first.id)) == 4
    assert h.store.model_usage_expired(h.workspace, at) == ()
    expired = h.store.model_usage_expired(h.workspace, at + timedelta(minutes=4))
    assert {value.id for value in expired} == {values[3].id, values[4].id}


def test_atomic_rollback_restores_enrollment_credentials_policies_and_liabilities(models):
    h = models
    value = usage(h)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.save_model_resource_policy(
            ModelResourcePolicy(workspace_id=h.workspace, version=1), 0
        )
        h.store.insert_model_usage(value)
        h.store.insert_project_model_credential(
            ProjectModelCredential(
                workspace_id=h.workspace,
                project_id=h.first.id,
                model_id=h.model.id,
                revision=1,
                encrypted_secret="ciphertext",
                created_by=h.owner,
            )
        )
        h.store.update_project_model(h.model.model_copy(update={"version": 2, "enabled": False}), 1)
        raise RuntimeError("Rollback all shared authority")
    assert h.store.model_resource_policy(h.workspace) is None
    assert h.store.model_usage_entries(h.workspace) == ()
    assert h.store.project_model_credential(h.workspace, h.first.id, h.model.id, 1) is None
    assert h.store.project_model(h.workspace, h.first.id, h.model.id) == h.model


def test_command_receipts_are_readonly_independent_copies(models):
    store = models.store
    assert store.command_receipt("models:test", "enroll") is None
    store.execute_once("models:test", "enroll", "a" * 64, lambda: {"safe": {"id": "model"}})
    receipt = store.command_receipt("models:test", "enroll")
    assert receipt == {"safe": {"id": "model"}}
    receipt["safe"]["id"] = "mutated"
    assert store.command_receipt("models:test", "enroll") == {"safe": {"id": "model"}}
    assert store.command_receipt("models:other", "enroll") is None


def test_zero_cost_unknown_call_keeps_capacity_until_known_resolution(models):
    h = models
    value = usage(h, status="unknown", reserved_microusd=0, held_microusd=0)
    h.store.insert_model_usage(value)
    assert h.store.model_usage_totals(h.workspace, h.first.id, value.started_at).active_calls == 1
    resolved = value.model_copy(update={"version": 2, "status": "settled"})
    h.store.update_model_usage(resolved, 1)
    assert h.store.model_usage_totals(h.workspace, h.first.id, value.started_at).active_calls == 0


def test_late_charge_after_membership_loss_requires_exact_reconciled_dispatch_context(models):
    h = models
    original = usage(h, status="unknown")
    h.store.insert_model_usage(original)
    source = ModelUsage.model_validate(
        original.model_dump()
        | {
            "version": 2,
            "status": "reconciled",
            "held_microusd": 0,
            "charged_microusd": 25,
            "reconciliation_by": h.owner,
            "reconciliation_at": original.deadline_at,
            "reconciliation_reason": "Provider invoice checked",
            "reconciliation_evidence": "Invoice item 1234 inspected",
        }
    )
    h.store.update_model_usage(source, 1)
    h.store.delete_membership(h.owner, h.workspace)
    adjustment = original.model_copy(
        update={
            "id": uuid4(),
            "phase": f"late_{source.id.hex}_1",
            "status": "settled",
            "reserved_microusd": 0,
            "held_microusd": 0,
            "charged_microusd": 20,
            "error_code": "late_provider_charge",
        }
    )
    for changes in (
        {"phase": "late_invalid"},
        {"phase": f"late_{uuid4().hex}_1"},
        {"phase": f"late_{source.id.hex}_0"},
        {"status": "reserved"},
        {"reserved_microusd": 1},
        {"charged_microusd": 0},
        {"held_microusd": 1},
        {"error_code": None},
        {"operation_id": uuid4()},
        {"model": "other-model"},
        {"requested_by": h.member},
        {"model_version": 0},
        {"endpoint_snapshot": {}},
        {"input_rate": Decimal(5)},
        {"started_at": original.started_at + timedelta(seconds=1)},
        {"reconciliation_reason": "Unrelated source evidence"},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_model_usage(adjustment.model_copy(update={"id": uuid4(), **changes}))
    h.store.insert_model_usage(adjustment)
    assert h.store.model_usage(h.workspace, h.first.id, adjustment.id) == adjustment
    assert h.store.model_usage_totals(h.workspace, None, original.started_at).charged_lifetime == 45
    assert h.store.model_usage(h.workspace, h.first.id, source.id) == source


@pytest.mark.postgres
def test_sql_constraints_enforce_accounting_and_composite_credential_scope(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = seed_native_projects(store)
        h.model = ProjectModel(
            workspace_id=h.workspace,
            project_id=h.first.id,
            template_id="local",
            label="Local",
            created_by=h.owner,
        )
        store.insert_project_model(h.model)
        value = usage(h)
        store.insert_model_usage(value)
        for statement in (
            "UPDATE model_usage SET charged_microusd=50 WHERE id=%s",
            "UPDATE model_usage SET status='settled' WHERE id=%s",
            "UPDATE model_usage SET version=2 WHERE id=%s",
            "UPDATE model_usage SET held_microusd=101 WHERE id=%s",
        ):
            with pytest.raises(psycopg.errors.CheckViolation), store.transaction():
                store.connection.execute(statement, (value.id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation), store.transaction():
            store.connection.execute(
                "UPDATE model_usage SET project_id=%s,snapshot="
                "jsonb_set(snapshot,'{project_id}',to_jsonb(%s::text)) WHERE id=%s",
                (h.second.id, str(h.second.id), value.id),
            )
        store.close()
        assert store.model_usage(h.workspace, h.first.id, value.id) == value
    finally:
        store.close()
