"""Durable per-call admission and settlement across project and workspace ceilings."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING
from typing import Any
from uuid import UUID

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.model_routing import TextGenerationResult
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_models import (
    ModelResourcePolicy,
    ModelUsage,
    ReconcileModelUsage,
    UpdateModelResourcePolicy,
    UsageTotals,
)
from simon.domain.ports import Store
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_projects import NativeProjectService


@dataclass(frozen=True)
class UsageOutcome:
    charged_microusd: int
    held_microusd: int
    input_tokens: int | None
    output_tokens: int | None


class ModelUsageService:
    def __init__(self, store: Store, projects: NativeProjectService) -> None:
        self.store, self.projects = store, projects

    def _access(self, actor: ActorContext, project_id: UUID, *, write: bool = False) -> None:
        if not isinstance(actor, ActorContext):
            raise AuthorizationError("Model resource controls require a human session.")
        project = self.projects._project(actor, project_id, write=write, owner=write)
        if write:
            self.projects._active(project)

    def _workspace_owner(self, actor: ActorContext) -> bool:
        return self.projects._workspace(actor, write=True).role == "owner"

    def policy(self, workspace_id: UUID, project_id: UUID | None = None) -> ModelResourcePolicy:
        return self.store.model_resource_policy(workspace_id, project_id) or ModelResourcePolicy(
            workspace_id=workspace_id,
            project_id=project_id,
            updated_at=datetime(1970, 1, 1, tzinfo=UTC),
        )

    def _audit(self, actor: ActorContext, project_id: UUID, event: str, **details: Any) -> None:
        self.projects.audit.record(
            event_type="native.model_usage." + event,
            actor=actor,
            resource_type="model_usage",
            resource_id=str(details.get("usage_id", project_id)),
            payload={"project_id": str(project_id), **details},
        )

    def _save(self, record: ModelUsage, **changes: Any) -> ModelUsage:
        value = ModelUsage.model_validate(
            {**record.model_dump(), **changes, "version": record.version + 1}
        )
        self.store.update_model_usage(value, record.version)
        return value

    def recover(self, workspace_id: UUID) -> None:
        """Expire unstarted holds and expose interrupted dispatches without resending."""
        now = utc_now()
        for entry in self.store.model_usage_expired(workspace_id, now):
            reserved = entry.status == "reserved"
            self._save(
                entry,
                status="released" if reserved else "unknown",
                held_microusd=0 if reserved else entry.held_microusd,
                finished_at=now,
                error_code="reservation_expired" if reserved else "dispatch_interrupted",
            )

    @staticmethod
    def _ceiling(
        policy: ModelResourcePolicy, totals: UsageTotals, *, addition: int = 0, calls: int = 0
    ) -> None:
        for label, used, limit in (
            ("lifetime", totals.charged_lifetime, policy.lifetime_limit_microusd),
            ("daily", totals.charged_day, policy.daily_limit_microusd),
            ("monthly", totals.charged_month, policy.monthly_limit_microusd),
        ):
            if used + totals.held_microusd + addition > limit:
                raise InvalidTransitionError(f"The {label} model spending ceiling is exhausted.")
        if totals.active_calls + calls > policy.max_concurrent_calls:
            raise InvalidTransitionError("The concurrent model-call allowance is exhausted.")

    def view(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self.recover(actor.workspace_id)
            manageable = False
            try:
                self._access(actor, project_id, write=True)
                manageable = True
            except (AuthorizationError, InvalidTransitionError):
                pass
            workspace_owner = self.projects._workspace(actor).role == "owner"
            now = utc_now()
            return {
                "project_policy": self.policy(actor.workspace_id, project_id).model_dump(
                    mode="json"
                ),
                "workspace_policy": self.policy(actor.workspace_id).model_dump(mode="json")
                if manageable or workspace_owner
                else None,
                "project_totals": self.store.model_usage_totals(
                    actor.workspace_id, project_id, now
                ).model_dump(mode="json"),
                "workspace_totals": self.store.model_usage_totals(
                    actor.workspace_id, None, now
                ).model_dump(mode="json")
                if manageable or workspace_owner
                else None,
                "can_manage": manageable,
                "can_manage_workspace": workspace_owner and manageable,
                "can_reconcile": workspace_owner,
            }

    def update_policy(
        self,
        actor: ActorContext,
        project_id: UUID,
        command: UpdateModelResourcePolicy,
        *,
        workspace: bool = False,
    ) -> ModelResourcePolicy:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            if workspace and not self._workspace_owner(actor):
                raise AuthorizationError("Only a workspace owner can change its model ceiling.")
            if workspace and (
                command.allow_paid
                or command.allow_cloud
                or command.planning_model_id is not None
                or command.review_model_id is not None
            ):
                raise ValidationError("Model permissions and routes belong to project settings.")
            scope = None if workspace else project_id

            def update() -> ModelResourcePolicy:
                self.recover(actor.workspace_id)
                current = self.policy(actor.workspace_id, scope)
                self.projects._version(current.version, command.expected_version)
                policy = ModelResourcePolicy(
                    workspace_id=actor.workspace_id,
                    project_id=scope,
                    version=current.version + 1,
                    **command.model_dump(exclude={"idempotency_key", "expected_version"}),
                )
                totals = self.store.model_usage_totals(actor.workspace_id, scope, utc_now())
                # Provider overruns can exceed a previously valid ceiling. Preserve
                # that liability without making unchanged limits block an emergency
                # pause or permission revocation. Dispatch still checks every ceiling.
                for field, minimum in (
                    ("lifetime_limit_microusd", totals.charged_lifetime + totals.held_microusd),
                    ("daily_limit_microusd", totals.charged_day + totals.held_microusd),
                    ("monthly_limit_microusd", totals.charged_month + totals.held_microusd),
                    ("max_concurrent_calls", totals.active_calls),
                ):
                    if (
                        getattr(policy, field) != getattr(current, field)
                        and getattr(policy, field) < minimum
                    ):
                        raise InvalidTransitionError(
                            "A changed model ceiling cannot discard existing usage or reservations."
                        )
                for model_id in (policy.planning_model_id, policy.review_model_id):
                    if model_id is not None:
                        model = self.store.project_model(actor.workspace_id, project_id, model_id)
                        if model is None or not model.enabled:
                            raise ValidationError("Choose an enabled model in this project.")
                self.store.save_model_resource_policy(policy, current.version)
                self._audit(
                    actor,
                    project_id,
                    "policy_updated",
                    scope="workspace" if workspace else "project",
                    version=policy.version,
                )
                return policy

            return self.projects._once(
                actor, f"model_policy:{scope or 'workspace'}", command, ModelResourcePolicy, update
            )

    def reserve(
        self,
        actor: ActorContext,
        project_id: UUID,
        operation_id: UUID,
        calls: tuple[ModelUsage, ...],
    ) -> tuple[ModelUsage, ...]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            if not 1 <= len(calls) <= 8 or len({item.phase for item in calls}) != len(calls):
                raise ValidationError("Reserve a bounded set of distinct model calls.")
            for call in calls:
                if (
                    call.workspace_id != actor.workspace_id
                    or call.project_id != project_id
                    or call.operation_id != operation_id
                    or call.requested_by != actor.actor_id
                    or call.status != "reserved"
                    or call.charged_microusd
                    or call.held_microusd != call.reserved_microusd
                    or call.model_id is None
                    or call.deadline_at <= utc_now()
                ):
                    raise ValidationError("Invalid model reservation scope or initial state.")
            existing = self.store.model_usage_for_operation(
                actor.workspace_id, project_id, operation_id
            )
            if existing:
                immutable = {
                    "workspace_id",
                    "project_id",
                    "operation_id",
                    "phase",
                    "requested_by",
                    "model_id",
                    "model_version",
                    "credential_revision",
                    "template_id",
                    "model",
                    "endpoint_fingerprint",
                    "endpoint_snapshot",
                    "input_rate",
                    "output_rate",
                    "reserved_microusd",
                }
                expected = {r.phase: r.model_dump(include=immutable) for r in calls}
                actual = {r.phase: r.model_dump(include=immutable) for r in existing}
                if expected != actual:
                    raise IdempotencyConflictError(
                        "This model operation already has different calls."
                    )
                return existing
            self.recover(actor.workspace_id)
            total = sum(item.reserved_microusd for item in calls)
            now = utc_now()
            for scope in (None, project_id):
                policy = self.policy(actor.workspace_id, scope)
                if policy.paused:
                    raise InvalidTransitionError("Model dispatch is paused by resource policy.")
                if total > policy.per_operation_limit_microusd:
                    raise InvalidTransitionError(
                        "This operation exceeds its model spending ceiling."
                    )
                self._ceiling(
                    policy,
                    self.store.model_usage_totals(actor.workspace_id, scope, now),
                    addition=total,
                    calls=len(calls),
                )
            for call in calls:
                self._model_current(actor, project_id, call)
                self.store.insert_model_usage(call)
            self._audit(
                actor,
                project_id,
                "reserved",
                operation_id=str(operation_id),
                usage_ids=[str(call.id) for call in calls],
                reserved_microusd=total,
            )
            return calls

    def _model_current(self, actor: ActorContext, project_id: UUID, usage: ModelUsage) -> None:
        model = (
            self.store.project_model(actor.workspace_id, project_id, usage.model_id)
            if usage.model_id is not None
            else None
        )
        if (
            model is None
            or not model.enabled
            or model.version != usage.model_version
            or model.credential_revision != usage.credential_revision
        ):
            raise InvalidTransitionError("Model enrollment or credentials changed before dispatch.")
        policy = self.policy(actor.workspace_id, project_id)
        if not policy.allow_cloud and not usage.endpoint_snapshot.get("local", False):
            raise InvalidTransitionError("Cloud inference is not authorized for this project.")
        if not policy.allow_paid and (usage.input_rate or usage.output_rate):
            raise InvalidTransitionError("Paid inference is not authorized for this project.")
        if usage.phase != "qualification" and (
            model.qualification_status != "ready"
            or model.qualification_fingerprint != usage.endpoint_fingerprint
        ):
            raise InvalidTransitionError("The chosen model needs a current capability test.")

    def _entry(self, workspace_id: UUID, project_id: UUID, usage_id: UUID) -> ModelUsage:
        result = self.store.model_usage(workspace_id, project_id, usage_id)
        if result is None:
            raise NotFoundError("Model usage entry not found.")
        return result

    def dispatch(self, actor: ActorContext, project_id: UUID, usage_id: UUID) -> ModelUsage:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            record = self._entry(actor.workspace_id, project_id, usage_id)
            if record.requested_by != actor.actor_id or record.status != "reserved":
                raise InvalidTransitionError("This model call cannot be dispatched again.")
            if record.deadline_at <= utc_now():
                raise InvalidTransitionError("The model reservation expired before dispatch.")
            self._model_current(actor, project_id, record)
            for scope in (None, project_id):
                policy = self.policy(actor.workspace_id, scope)
                if policy.paused:
                    raise InvalidTransitionError("Model dispatch is paused by resource policy.")
                self._ceiling(
                    policy, self.store.model_usage_totals(actor.workspace_id, scope, utc_now())
                )
                operation = self.store.model_usage_for_operation(
                    actor.workspace_id, project_id, record.operation_id
                )
                if (
                    sum(r.charged_microusd + r.held_microusd for r in operation)
                    > policy.per_operation_limit_microusd
                ):
                    raise InvalidTransitionError("The operation spending ceiling changed.")
            result = self._save(record, status="dispatched")
            self._audit(actor, project_id, "dispatched", usage_id=str(record.id))
            return result

    def release(self, workspace_id: UUID, project_id: UUID, usage_id: UUID) -> ModelUsage:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(workspace_id):
            entry = self._entry(workspace_id, project_id, usage_id)
            if entry.status != "reserved":
                return entry
            return self._save(entry, status="released", held_microusd=0, finished_at=utc_now())

    def settle(
        self,
        workspace_id: UUID,
        project_id: UUID,
        usage_id: UUID,
        result: TextGenerationResult | None,
        *,
        unknown: bool = False,
        error_code: str | None = None,
    ) -> ModelUsage:
        # Internal accounting survives access loss, cancellation and key revocation.
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(workspace_id):
            entry = self._entry(workspace_id, project_id, usage_id)
            if entry.status == "reserved":
                raise InvalidTransitionError("An undispatched call must be released, not settled.")
            if entry.status in {"settled", "released"}:
                return entry
            uncertain = unknown or (
                result is not None
                and (
                    result.input_tokens is None
                    or result.output_tokens is None
                    or result.endpoint_id != entry.endpoint_snapshot.get("id")
                )
            )
            amount = 0
            if result is not None and not uncertain:
                assert result.input_tokens is not None and result.output_tokens is not None
                amount = int(
                    (
                        result.input_tokens * entry.input_rate
                        + result.output_tokens * entry.output_rate
                    ).to_integral_value(rounding=ROUND_CEILING)
                )
            if amount > 9_223_372_036_854_775_807:
                uncertain, error_code = True, "provider_usage_out_of_range"
            if entry.status == "reconciled":
                if not uncertain and amount > entry.charged_microusd:
                    self._late_charge(entry, amount - entry.charged_microusd)
                return entry
            return self._save(
                entry,
                status="unknown" if uncertain else "settled",
                held_microusd=entry.held_microusd if uncertain else 0,
                charged_microusd=entry.charged_microusd if uncertain else amount,
                input_tokens=None if uncertain or result is None else result.input_tokens,
                output_tokens=None if uncertain or result is None else result.output_tokens,
                reported_model=self._reported_model(result),
                finished_at=utc_now(),
                error_code=error_code
                or (
                    "provider_usage_exceeded_reservation"
                    if amount > entry.reserved_microusd
                    else None
                ),
            )

    @staticmethod
    def _reported_model(result: TextGenerationResult | None) -> str | None:
        if result is None or not 1 <= len(result.model) <= 256 or "\x00" in result.model:
            return None
        try:
            result.model.encode("utf-8")
        except UnicodeError:
            return None
        return result.model

    def _late_charge(self, entry: ModelUsage, amount: int) -> None:
        prefix = "late_" + entry.id.hex + "_"
        adjustments = [
            r
            for r in self.store.model_usage_for_operation(
                entry.workspace_id, entry.project_id, entry.operation_id
            )
            if r.phase.startswith(prefix)
        ]
        difference = amount - sum(r.charged_microusd for r in adjustments)
        if difference <= 0:
            return
        values = entry.model_dump(
            exclude={
                "id",
                "version",
                "phase",
                "status",
                "reserved_microusd",
                "held_microusd",
                "charged_microusd",
                "reconciliation_by",
                "reconciliation_at",
                "reconciliation_reason",
                "reconciliation_evidence",
                "input_tokens",
                "output_tokens",
                "finished_at",
                "error_code",
            }
        )
        adjustment = ModelUsage(
            **values,
            phase=prefix + str(len(adjustments) + 1),
            status="settled",
            reserved_microusd=0,
            held_microusd=0,
            charged_microusd=difference,
            finished_at=utc_now(),
            error_code="late_provider_charge",
        )
        self.store.insert_model_usage(adjustment)
        from simon.domain.models import Channel

        actor = ActorContext(
            actor_id=entry.requested_by,
            workspace_id=entry.workspace_id,
            scopes=frozenset(),
            channel=Channel.API,
        )
        self._audit(
            actor,
            entry.project_id,
            "reconciliation_adjustment",
            usage_id=str(entry.id),
            adjustment_id=str(adjustment.id),
            charged_microusd=difference,
        )

    def list_usage(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self.projects._page(offset, limit)
            self.recover(actor.workspace_id)
            items = self.store.model_usage_entries(actor.workspace_id, project_id, offset, limit)
            more = self.store.model_usage_entries(actor.workspace_id, project_id, offset + limit, 1)
            return {
                "items": [
                    item.model_dump(mode="json", exclude={"endpoint_snapshot"}) for item in items
                ],
                "has_more": bool(more),
            }

    def reconcile(
        self,
        actor: ActorContext,
        project_id: UUID,
        usage_id: UUID,
        command: ReconcileModelUsage,
    ) -> ModelUsage:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            if not self._workspace_owner(actor):
                raise AuthorizationError("Only a workspace owner can reconcile shared model usage.")

            def resolve() -> ModelUsage:
                self.recover(actor.workspace_id)
                entry = self._entry(actor.workspace_id, project_id, usage_id)
                self.projects._version(entry.version, command.expected_version)
                if entry.status != "unknown" or entry.deadline_at > utc_now():
                    raise InvalidTransitionError(
                        "Only an expired uncertain call can be reconciled."
                    )
                if command.charged_microusd < entry.charged_microusd:
                    raise ValidationError("Reconciliation cannot erase already known charges.")
                result = self._save(
                    entry,
                    status="reconciled",
                    held_microusd=0,
                    charged_microusd=command.charged_microusd,
                    finished_at=utc_now(),
                    reconciliation_by=actor.actor_id,
                    reconciliation_at=utc_now(),
                    reconciliation_reason=command.reason,
                    reconciliation_evidence=command.evidence,
                )
                self._audit(
                    actor,
                    project_id,
                    "reconciled",
                    usage_id=str(entry.id),
                    charged_microusd=result.charged_microusd,
                )
                return result

            self.projects._once(
                actor, f"model_usage:{usage_id}:reconcile", command, ModelUsage, resolve
            )
            return self._entry(actor.workspace_id, project_id, usage_id)

    def operation_outcome(
        self, workspace_id: UUID, project_id: UUID, operation_id: UUID
    ) -> UsageOutcome:
        records = self.store.model_usage_for_operation(workspace_id, project_id, operation_id)
        counted = [r for r in records if r.status != "released"]
        complete = bool(counted) and all(
            r.input_tokens is not None and r.output_tokens is not None for r in counted
        )
        return UsageOutcome(
            charged_microusd=sum(r.charged_microusd for r in records),
            held_microusd=sum(r.held_microusd for r in records),
            input_tokens=sum(r.input_tokens or 0 for r in counted) if complete else None,
            output_tokens=sum(r.output_tokens or 0 for r in counted) if complete else None,
        )
