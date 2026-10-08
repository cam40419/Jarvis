"""Shared model admission preserves real liabilities across races and interruptions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.model_routing import TextGenerationResult
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_models import (
    ModelUsage,
    ReconcileModelUsage,
    UpdateModelResourcePolicy,
)
from simon.services.identity import ROLE_SCOPES
from tests.unit.test_project_models import enroll, setup_models


@pytest.fixture
def ledger():
    h = setup_models(paid=True)
    row = enroll(h)
    h.model_id = UUID(row["id"])
    binding = h.models.resolve(h.owner_actor, h.first.id, h.model_id, require_qualified=False)
    qualified = binding.model.model_copy(
        update={
            "version": binding.model.version + 1,
            "qualification_status": "ready",
            "qualification_fingerprint": binding.fingerprint,
            "qualified_at": utc_now(),
        }
    )
    h.store.update_project_model(qualified, binding.model.version)
    h.binding = h.models.resolve(h.owner_actor, h.first.id, h.model_id)
    return h


def call(h, *, operation=None, phase="generation", amount=500, started=None, **changes):
    started = started or utc_now()
    b = h.binding
    return ModelUsage(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "operation_id": operation or uuid4(),
            "phase": phase,
            "requested_by": h.owner_actor.actor_id,
            "model_id": b.model.id,
            "model_version": b.model.version,
            "credential_revision": b.model.credential_revision,
            "template_id": b.model.template_id,
            "model": b.endpoint.model,
            "endpoint_fingerprint": b.fingerprint,
            "endpoint_snapshot": b.endpoint.model_dump(mode="json"),
            "input_rate": Decimal(1),
            "output_rate": Decimal(2),
            "reserved_microusd": amount,
            "held_microusd": amount,
            "started_at": started,
            "deadline_at": started + timedelta(minutes=5),
            **changes,
        }
    )


def reserve(h, *entries):
    return h.usage.reserve(h.owner_actor, h.first.id, entries[0].operation_id, entries)


def result(h, **changes):
    return TextGenerationResult(
        **{
            "endpoint_id": h.binding.endpoint.id,
            "model": h.binding.endpoint.model,
            "text": "A bounded result.",
            "input_tokens": 100,
            "output_tokens": 40,
            **changes,
        }
    )


def set_policy(h, *, workspace=False, **changes):
    scope = None if workspace else h.first.id
    current = h.usage.policy(h.workspace, scope)
    values = current.model_dump(exclude={"workspace_id", "project_id", "version", "updated_at"})
    return h.usage.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateModelResourcePolicy(
            **{
                **values,
                **changes,
                "expected_version": current.version,
                "idempotency_key": f"policy-{uuid4()}",
            }
        ),
        workspace=workspace,
    )


def expired(h, entry, *, status="unknown", charged=0):
    """Insert an old, immutable dispatch snapshot as if restored after interruption."""
    at = utc_now() - timedelta(days=35)
    old = entry.model_copy(
        update={
            "started_at": at,
            "deadline_at": at + timedelta(minutes=5),
            "status": status,
            "charged_microusd": charged,
        }
    )
    h.store.insert_model_usage(old)
    return old


def reconciliation(entry, amount=120, **changes):
    return ReconcileModelUsage(
        **{
            "expected_version": entry.version,
            "idempotency_key": f"reconcile-{uuid4()}",
            "charged_microusd": amount,
            "reason": "Reviewed provider usage receipt.",
            "evidence": "Provider request receipt reference 12345.",
            **changes,
        }
    )


def test_generation_and_review_admitted_atomically_and_settled_independently(ledger):
    h = ledger
    generation = call(h)
    review = call(h, operation=generation.operation_id, phase="review", amount=800)
    assert reserve(h, generation, review) == (generation, review)
    h.usage.dispatch(h.owner_actor, h.first.id, generation.id)
    paid = h.usage.settle(h.workspace, h.first.id, generation.id, result(h))
    assert paid.charged_microusd == 180 and paid.held_microusd == 0
    h.usage.dispatch(h.owner_actor, h.first.id, review.id)
    unknown = h.usage.settle(h.workspace, h.first.id, review.id, None, unknown=True)
    assert unknown.status == "unknown" and unknown.held_microusd == 800
    totals = h.store.model_usage_totals(h.workspace, None, utc_now())
    assert totals.charged_lifetime == 180 and totals.held_microusd == 800
    assert totals.active_calls == 1
    outcome = h.usage.operation_outcome(h.workspace, h.first.id, generation.operation_id)
    assert outcome.charged_microusd == 180 and outcome.held_microusd == 800
    assert outcome.input_tokens is None


@pytest.mark.parametrize("scope", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "lifetime_limit_microusd",
        "daily_limit_microusd",
        "monthly_limit_microusd",
        "per_operation_limit_microusd",
    ],
)
def test_every_applicable_ceiling_rejects_entire_package_before_any_dispatch(ledger, scope, field):
    h = ledger
    set_policy(h, workspace=scope, **{field: 700})
    first = call(h)
    second = call(h, operation=first.operation_id, phase="review")
    with pytest.raises(InvalidTransitionError):
        reserve(h, first, second)
    assert h.store.model_usage_for_operation(h.workspace, h.first.id, first.operation_id) == ()
    assert h.calls == []


def test_simultaneous_operations_cannot_spend_same_last_allowance(ledger):
    h = ledger
    set_policy(h, workspace=True, lifetime_limit_microusd=700)
    gate = Barrier(2)

    def attempt(entry):
        gate.wait(timeout=5)
        try:
            reserve(h, entry)
            return True
        except InvalidTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [call(h), call(h)]))
    assert sorted(outcomes) == [False, True]
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).held_microusd == 500


def test_reservation_retry_is_idempotent_but_changed_context_is_rejected(ledger):
    h = ledger
    original = call(h)
    reserve(h, original)
    retry = original.model_copy(update={"id": uuid4()})
    assert reserve(h, retry) == (original,)
    with pytest.raises(IdempotencyConflictError):
        reserve(h, original.model_copy(update={"reserved_microusd": 501, "held_microusd": 501}))
    h.usage.dispatch(h.owner_actor, h.first.id, original.id)
    with pytest.raises(InvalidTransitionError):
        h.usage.dispatch(h.owner_actor, h.first.id, original.id)


def test_only_undispatched_calls_can_release_their_holds(ledger):
    h = ledger
    one, two = call(h), call(h)
    reserve(h, one)
    reserve(h, two)
    h.usage.dispatch(h.owner_actor, h.first.id, one.id)
    assert h.usage.release(h.workspace, h.first.id, one.id).held_microusd == 500
    released = h.usage.release(h.workspace, h.first.id, two.id)
    assert released.status == "released" and released.held_microusd == 0
    assert h.usage.release(h.workspace, h.first.id, two.id) == released
    with pytest.raises(InvalidTransitionError):
        h.usage.dispatch(h.owner_actor, h.first.id, two.id)


def test_expired_holds_recover_without_redispatch_or_hidden_budget_reset(ledger):
    h = ledger
    never_sent = expired(h, call(h), status="reserved")
    sent = expired(h, call(h), status="dispatched")
    h.usage.view(h.owner_actor, h.first.id)
    assert h.store.model_usage(h.workspace, h.first.id, never_sent.id).status == "released"
    recovered = h.store.model_usage(h.workspace, h.first.id, sent.id)
    assert recovered.status == "unknown" and recovered.held_microusd == 500
    totals = h.store.model_usage_totals(h.workspace, None, utc_now())
    assert totals.held_microusd == 500 and totals.active_calls == 1
    set_policy(h, workspace=True, monthly_limit_microusd=600)
    with pytest.raises(InvalidTransitionError):
        reserve(h, call(h, amount=101))


@pytest.mark.parametrize(
    "field",
    [
        "lifetime_limit_microusd",
        "daily_limit_microusd",
        "monthly_limit_microusd",
        "max_concurrent_calls",
    ],
)
def test_policy_cannot_erase_existing_liability_or_active_admission(ledger, field):
    h = ledger
    first = call(h)
    second = call(h, operation=first.operation_id, phase="review")
    reserve(h, first, second)
    with pytest.raises(InvalidTransitionError):
        set_policy(h, **{field: 1 if field == "max_concurrent_calls" else 999})


@pytest.mark.parametrize("scope", [False, True])
def test_pause_fences_dispatch_even_after_reservation(ledger, scope):
    h = ledger
    entry = call(h)
    reserve(h, entry)
    set_policy(h, workspace=scope, paused=True)
    with pytest.raises(InvalidTransitionError):
        h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
    assert h.usage.release(h.workspace, h.first.id, entry.id).status == "released"


def test_known_usage_remains_accounted_when_owner_access_is_revoked(ledger):
    h = ledger
    entry = call(h)
    reserve(h, entry)
    h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
    h.store.delete_membership(h.owner_actor.actor_id, h.workspace)
    settled = h.usage.settle(h.workspace, h.first.id, entry.id, result(h))
    assert settled.charged_microusd == 180
    with pytest.raises(AuthorizationError):
        h.usage.view(h.owner_actor, h.first.id)


@pytest.mark.parametrize(
    "change", [{"input_tokens": None}, {"output_tokens": None}, {"endpoint_id": "unrelated"}]
)
def test_unverifiable_usage_holds_reservation(ledger, change):
    h = ledger
    entry = call(h)
    reserve(h, entry)
    h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
    settled = h.usage.settle(h.workspace, h.first.id, entry.id, result(h, **change))
    assert settled.status == "unknown" and settled.held_microusd == 500
    assert settled.input_tokens is None and settled.charged_microusd == 0


def test_reported_versioned_model_alias_is_recorded_without_losing_known_usage(ledger):
    h = ledger
    entry = call(h)
    reserve(h, entry)
    h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
    settled = h.usage.settle(h.workspace, h.first.id, entry.id, result(h, model="model-2026-10-08"))
    assert settled.status == "settled" and settled.charged_microusd == 180
    assert settled.model == h.binding.endpoint.model
    assert settled.reported_model == "model-2026-10-08"


def test_each_call_rounds_fractional_usage_up_without_floating_point_loss(ledger):
    h = ledger
    first = call(h, input_rate=Decimal("0.125"), output_rate=Decimal("0.3"))
    second = call(
        h,
        operation=first.operation_id,
        phase="review",
        input_rate=Decimal("0.125"),
        output_rate=Decimal("0.3"),
    )
    reserve(h, first, second)
    for entry in (first, second):
        h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
        assert (
            h.usage.settle(
                h.workspace, h.first.id, entry.id, result(h, input_tokens=1, output_tokens=1)
            ).charged_microusd
            == 1
        )
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).charged_lifetime == 2


def test_provider_overrun_is_charged_and_cannot_be_reset_by_repeated_settlement(ledger):
    h = ledger
    entry = call(h)
    reserve(h, entry)
    h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
    paid = h.usage.settle(h.workspace, h.first.id, entry.id, result(h, output_tokens=1000))
    assert (
        paid.charged_microusd == 2100 and paid.error_code == "provider_usage_exceeded_reservation"
    )
    assert h.usage.settle(h.workspace, h.first.id, entry.id, result(h)) == paid
    with pytest.raises(InvalidTransitionError):
        set_policy(h, lifetime_limit_microusd=1000)


def test_emergency_pause_and_permission_revocation_remain_available_after_overrun(ledger):
    h = ledger
    set_policy(h, lifetime_limit_microusd=500)
    entry = call(h)
    reserve(h, entry)
    h.usage.dispatch(h.owner_actor, h.first.id, entry.id)
    h.usage.settle(h.workspace, h.first.id, entry.id, result(h, output_tokens=1000))
    paused = set_policy(h, paused=True, allow_paid=False, allow_cloud=False)
    assert paused.paused and not paused.allow_paid and not paused.allow_cloud
    assert paused.lifetime_limit_microusd == 500
    set_policy(h, paused=False)
    with pytest.raises(InvalidTransitionError, match="ceiling"):
        reserve(h, call(h, amount=0))


def test_reconciliation_releases_only_unknown_hold_and_keeps_receipt_evidence(ledger):
    h = ledger
    entry = expired(h, call(h))
    command = reconciliation(entry, amount=200)
    paid = h.usage.reconcile(h.owner_actor, h.first.id, entry.id, command)
    assert paid.status == "reconciled" and paid.charged_microusd == 200 and not paid.held_microusd
    assert paid.reconciliation_by == h.owner_actor.actor_id
    assert paid.reconciliation_evidence == command.evidence
    assert h.usage.reconcile(h.owner_actor, h.first.id, entry.id, command) == paid
    totals = h.store.model_usage_totals(h.workspace, None, utc_now())
    assert totals.charged_lifetime == 200 and totals.charged_month == 0


def test_late_provider_charge_adds_conservative_adjustment_without_rewriting_owner_receipt(ledger):
    h = ledger
    entry = expired(h, call(h))
    manual = h.usage.reconcile(
        h.owner_actor, h.first.id, entry.id, reconciliation(entry, amount=100)
    )
    late = h.usage.settle(h.workspace, h.first.id, entry.id, result(h))
    assert late == manual
    records = h.store.model_usage_for_operation(h.workspace, h.first.id, entry.operation_id)
    adjustment = next(row for row in records if row.id != entry.id)
    assert adjustment.charged_microusd == 80 and adjustment.status == "settled"
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).charged_lifetime == 180
    h.usage.settle(h.workspace, h.first.id, entry.id, result(h))
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).charged_lifetime == 180
    h.usage.settle(h.workspace, h.first.id, entry.id, result(h, output_tokens=100))
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).charged_lifetime == 300
    records = h.store.model_usage_for_operation(h.workspace, h.first.id, entry.operation_id)
    assert len(records) == 3
    assert h.store.model_usage(h.workspace, h.first.id, adjustment.id) == adjustment
    assert h.store.model_usage(h.workspace, h.first.id, entry.id) == manual


def test_unknown_zero_cost_call_keeps_concurrency_until_reconciled(ledger):
    h = ledger
    entry = expired(h, call(h, amount=0))
    set_policy(h, max_concurrent_calls=1)
    with pytest.raises(InvalidTransitionError, match="concurrent"):
        reserve(h, call(h, amount=0))
    h.usage.reconcile(h.owner_actor, h.first.id, entry.id, reconciliation(entry, amount=0))
    assert reserve(h, call(h, amount=0))


def test_late_provider_charge_survives_original_owner_removal(ledger):
    h = ledger
    entry = expired(h, call(h))
    manual = h.usage.reconcile(
        h.owner_actor, h.first.id, entry.id, reconciliation(entry, amount=100)
    )
    h.store.delete_membership(h.owner_actor.actor_id, h.workspace)
    assert h.usage.settle(h.workspace, h.first.id, entry.id, result(h)) == manual
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).charged_lifetime == 180


def test_reconciliation_waits_for_deadline_and_refuses_known_charge_reduction(ledger):
    h = ledger
    current = call(h).model_copy(update={"status": "unknown"})
    h.store.insert_model_usage(current)
    with pytest.raises(InvalidTransitionError):
        h.usage.reconcile(h.owner_actor, h.first.id, current.id, reconciliation(current))
    old = expired(h, call(h), charged=100)
    with pytest.raises(ValidationError):
        h.usage.reconcile(h.owner_actor, h.first.id, old.id, reconciliation(old, amount=99))


def test_nonworkspace_owner_cannot_reconcile_even_as_project_owner(ledger):
    h = ledger
    from simon.domain.identity import Membership
    from simon.domain.native_projects import NativeProjectMember

    actor_id = uuid4()
    h.store.put_membership(Membership(actor_id=actor_id, workspace_id=h.workspace, role="member"))
    h.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=h.workspace, project_id=h.first.id, actor_id=actor_id, role="owner"
        )
    )
    actor = ActorContext(
        actor_id=actor_id,
        workspace_id=h.workspace,
        scopes=ROLE_SCOPES["member"],
        channel=Channel.API,
    )
    entry = expired(h, call(h))
    view = h.usage.view(actor, h.first.id)
    assert view["can_manage"] and not view["can_manage_workspace"] and not view["can_reconcile"]
    with pytest.raises(AuthorizationError):
        h.usage.reconcile(actor, h.first.id, entry.id, reconciliation(entry))


def test_usage_history_is_scoped_and_paginates_without_exposing_endpoint_snapshots(ledger):
    h = ledger
    for _ in range(4):
        expired(h, call(h))
    first = h.usage.list_usage(h.owner_actor, h.first.id, limit=2)
    second = h.usage.list_usage(h.owner_actor, h.first.id, offset=2, limit=2)
    assert first["has_more"] and not second["has_more"]
    assert len({row["id"] for row in first["items"] + second["items"]}) == 4
    assert all("endpoint_snapshot" not in row for row in first["items"])
    assert h.usage.list_usage(h.owner_actor, h.second.id)["items"] == []
    with pytest.raises(NotFoundError):
        h.usage.dispatch(h.owner_actor, h.second.id, UUID(first["items"][0]["id"]))
