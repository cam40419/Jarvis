"""Durable reviewed commitments, using the existing Store transaction and job journal."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from simon.adapters.external_action_providers import (
    ExternalActionProviders,
    provider_configuration_reasons,
    provider_fingerprint,
)
from simon.domain.errors import AuthorizationError, InvalidTransitionError, NotFoundError
from simon.domain.external_actions import (
    ExternalActionDraft,
    ExternalActionProposal,
    ExternalProviderDefinition,
    ExternalQuoteRequest,
    ManualActionResolution,
    ReconcileExternalAction,
)
from simon.domain.models import ActorContext, Channel, Job, JobStatus, utc_now
from simon.domain.ports import Store
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.integrations import IntegrationService

ACTION_JOB = "platform.external_action"
ACTION_RUN_JOB = "platform.external_action_run."
JOB_STATUS = {
    "pending": JobStatus.NEEDS_HUMAN,
    "executing": JobStatus.RUNNING,
    "accepted": JobStatus.WAITING,
    "succeeded": JobStatus.SUCCEEDED,
    "failed": JobStatus.FAILED,
    "unknown": JobStatus.NEEDS_HUMAN,
    "cancelled": JobStatus.CANCELLED,
    "expired": JobStatus.CANCELLED,
    "resolved": JobStatus.SUCCEEDED,
}


class ExternalActionService:
    def __init__(
        self,
        store: Store,
        audit: AuditService,
        definitions: Sequence[ExternalProviderDefinition],
        providers: ExternalActionProviders,
        *,
        integrations: IntegrationService | None = None,
    ) -> None:
        self.integrations = integrations
        self.store, self.audit, self.providers = store, audit, providers
        self.definitions = {value.id: value.model_copy(deep=True) for value in definitions}
        if len(self.definitions) != len(definitions):
            raise ValueError("External provider IDs must be unique")

    @staticmethod
    def _authorize(actor: ActorContext, *, write: bool = False, decision: bool = False) -> None:
        if ("jobs:write" if write else "jobs:read") not in actor.scopes:
            raise AuthorizationError("External action access is not authorized")
        if decision and actor.channel != Channel.API:
            raise AuthorizationError("External commitments require the authenticated review API")

    def _definition(
        self,
        actor: ActorContext,
        identifier: str,
    ) -> ExternalProviderDefinition | None:
        managed = self.integrations.external_connections(actor) if self.integrations else ()
        definition = next(
            (row for row in managed if row.id == identifier), self.definitions.get(identifier)
        )
        if definition is not None and (
            actor.workspace_id != definition.workspace_id
            or actor.actor_id not in definition.actor_ids
        ):
            raise NotFoundError("External provider is not available for this account")
        return definition

    def provider_statuses(self, actor: ActorContext) -> tuple[dict[str, Any], ...]:
        self._authorize(actor)
        return tuple(
            {
                "id": value.id,
                "name": value.name,
                "kind": value.kind,
                "available": not provider_configuration_reasons(value, self.providers.http.environ),
                "blockers": provider_configuration_reasons(value, self.providers.http.environ),
                "from_number": value.from_number if value.kind == "twilio" else None,
                "merchants": value.merchant_names if value.kind == "gateway" else {},
                "capabilities": ["prerecorded_phone_message"]
                if value.kind == "twilio"
                else [
                    "quote_lookup",
                    "purchase",
                    "booking",
                    "reservation",
                ],
            }
            for value in sorted(
                (
                    *self.definitions.values(),
                    *(self.integrations.external_connections(actor) if self.integrations else ()),
                ),
                key=lambda item: item.id,
            )
            if value.workspace_id == actor.workspace_id and actor.actor_id in value.actor_ids
        )

    def quote(self, actor: ActorContext, request: ExternalQuoteRequest) -> ExternalActionDraft:
        self._authorize(actor)
        definition = self._definition(actor, request.provider_id)
        if definition is None:
            raise ToolCatalogError("This account has no configured quote/availability provider")
        return self.providers.quote(definition, request)

    def _job(self, actor: ActorContext, identifier: UUID) -> Job:
        job = self.store.get_job(identifier)
        if (
            job is None
            or job.kind != ACTION_JOB
            or (job.workspace_id, job.created_by) != (actor.workspace_id, actor.actor_id)
        ):
            raise NotFoundError("External action not found")
        return job

    @staticmethod
    def _action(job: Job) -> ExternalActionProposal:
        value = (job.result or job.input).get("proposal")
        action = ExternalActionProposal.model_validate(value)
        if (action.id, action.actor_id, action.workspace_id) != (
            job.id,
            job.created_by,
            job.workspace_id,
        ):
            raise InvalidTransitionError("External action journal identity does not match")
        if (
            digest(action.draft.model_dump(mode="json")) != action.draft_digest
            or digest({"draft": action.draft_digest, "provider": action.provider_fingerprint})
            != action.review_digest
            or job.input["proposal"]["review_digest"] != action.review_digest
        ):
            raise InvalidTransitionError("External action review details changed in the journal")
        return action

    def _blockers(self, actor: ActorContext, action: ExternalActionProposal) -> tuple[str, ...]:
        try:
            definition = self._definition(actor, action.draft.provider_id)
        except NotFoundError:
            return ("Provider access was revoked for this account",)
        if definition is None:
            return ("No provider is configured for this action",)
        if provider_fingerprint(definition) != action.provider_fingerprint:
            return ("Provider configuration changed; prepare a new proposal for review",)
        reasons = self.providers.reasons(definition, action.draft)
        if (
            action.status in {"pending", "executing"}
            and action.draft.reservation
            and action.draft.reservation.start_at <= utc_now()
        ):
            reasons += ("The reviewed reservation start time has passed",)
        return reasons

    def get(self, actor: ActorContext, identifier: UUID) -> ExternalActionProposal:
        self._authorize(actor)
        action = self._action(self._job(actor, identifier))
        if action.status == "pending":
            action = action.model_copy(update={"blockers": self._blockers(actor, action)})
        return action

    def list_actions(
        self,
        actor: ActorContext,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[ExternalActionProposal, ...]:
        self._authorize(actor)
        if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
            raise ValueError("Action listing limits are out of range")
        return tuple(
            self.get(actor, job.id)
            for job in self.store.jobs(
                actor.workspace_id,
                actor.actor_id,
                ACTION_JOB,
                offset,
                limit,
            )
        )

    def propose(
        self,
        actor: ActorContext,
        draft: ExternalActionDraft,
        idempotency_key: str,
        *,
        run_id: UUID | None = None,
    ) -> ExternalActionProposal:
        self._authorize(actor, write=True)
        # Revalidate copies created by internal integrations before persisting them.
        draft = ExternalActionDraft.model_validate(draft.model_dump())
        definition = self._definition(actor, draft.provider_id)
        fingerprint = provider_fingerprint(definition) if definition else ""
        draft_hash = digest(draft.model_dump(mode="json"))
        review_hash = digest({"draft": draft_hash, "provider": fingerprint})
        now = utc_now()
        action = ExternalActionProposal(
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
            draft=draft,
            draft_digest=draft_hash,
            review_digest=review_hash,
            provider_fingerprint=fingerprint,
            provider_name=definition.name if definition else draft.provider_id,
            provider_kind=definition.kind if definition else "unconfigured",
            from_number=definition.from_number if definition else None,
            blockers=self.providers.reasons(definition, draft)
            if definition
            else ("No provider is configured for this action",),
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        job = Job(
            id=action.id,
            workspace_id=actor.workspace_id,
            created_by=actor.actor_id,
            kind=ACTION_JOB,
            status=JobStatus.NEEDS_HUMAN,
            idempotency_key=digest({"actor": str(actor.actor_id), "key": idempotency_key}),
            input={
                "proposal": action.model_dump(mode="json"),
                "rank": -int(now.timestamp() * 1000),
            },
            input_digest=digest(
                {
                    "draft": draft_hash,
                    "provider": fingerprint,
                    "run_id": str(run_id) if run_id else None,
                }
            ),
        )
        with self.store.transaction(actor.workspace_id):
            saved, created = self.store.create_job(job)
            if created:
                if run_id is not None:
                    link = {"action_id": str(action.id), "run_id": str(run_id)}
                    self.store.create_job(
                        Job(
                            workspace_id=actor.workspace_id,
                            created_by=actor.actor_id,
                            kind=ACTION_RUN_JOB + run_id.hex,
                            status=JobStatus.SUCCEEDED,
                            idempotency_key=str(action.id),
                            input=link,
                            input_digest=digest(link),
                        )
                    )
                self._audit(actor, action, "proposed")
            return self._action(saved)

    def list_for_run(self, actor: ActorContext, run_id: UUID) -> tuple[ExternalActionProposal, ...]:
        """Read the run's durable secondary index without scanning other actors or runs."""
        self._authorize(actor)
        result: list[ExternalActionProposal] = []
        offset = 0
        while True:
            links = self.store.jobs(
                actor.workspace_id, actor.actor_id, ACTION_RUN_JOB + run_id.hex, offset, 100
            )
            for link in links:
                action = self.get(actor, UUID(link.input["action_id"]))
                if action.run_id != run_id or link.input.get("run_id") != str(run_id):
                    raise InvalidTransitionError("External action run index does not match")
                result.append(action)
            if len(links) < 100:
                return tuple(result)
            offset += len(links)

    def _audit(self, actor: ActorContext, action: ExternalActionProposal, event: str) -> None:
        self.audit.record(
            event_type="external_action." + event,
            actor=actor,
            resource_type="external_action",
            resource_id=str(action.id),
            payload={
                "kind": action.draft.kind,
                "status": action.status,
                "review_digest": action.review_digest,
            },
        )

    def _save(self, actor: ActorContext, job: Job, action: ExternalActionProposal) -> None:
        self.store.transition_job(
            job.id,
            job.version,
            JOB_STATUS[action.status],
            {"proposal": action.model_dump(mode="json")},
            "external_outcome_unknown" if action.status == "unknown" else None,
        )
        self._audit(actor, action, action.status)

    def _revalidate(
        self,
        actor: ActorContext,
        revalidate: Callable[[], ActorContext],
    ) -> ActorContext:
        current = revalidate()
        if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
            raise AuthorizationError("External action access changed")
        self._authorize(current, write=True, decision=True)
        return current

    def decide(
        self,
        actor: ActorContext,
        identifier: UUID,
        *,
        review_digest: str,
        confirm: bool,
        revalidate: Callable[[], ActorContext],
    ) -> ExternalActionProposal:
        self._authorize(actor, write=True, decision=True)
        with self.store.transaction(actor.workspace_id):
            actor = self._revalidate(actor, revalidate)
            job = self._job(actor, identifier)
            action = self._action(job)
            if not hmac.compare_digest(review_digest, action.review_digest):
                raise InvalidTransitionError(
                    "The reviewed action changed; reload its exact details"
                )
            if action.status != "pending":
                return action  # Includes executing and unknown: never dispatch a retry.
            now = utc_now()
            if not confirm or action.expires_at <= now:
                action = action.model_copy(
                    update={
                        "status": "cancelled" if not confirm else "expired",
                        "updated_at": now,
                    }
                )
                self._save(actor, job, action)
                return action
            blockers = self._blockers(actor, action)
            if blockers:
                return action.model_copy(update={"blockers": blockers})
            action = action.model_copy(
                update={
                    "status": "executing",
                    "claimed_at": now,
                    "updated_at": now,
                    "blockers": (),
                }
            )
            self._save(actor, job, action)
        # The claim is committed before network I/O. A crash here leaves an
        # executing action requiring reconciliation; no automatic redispatch.
        return self._dispatch(actor, action, revalidate)

    def _dispatch(
        self,
        actor: ActorContext,
        action: ExternalActionProposal,
        revalidate: Callable[[], ActorContext],
    ) -> ExternalActionProposal:
        dispatched = False
        try:
            actor = self._revalidate(actor, revalidate)
            definition = self._definition(actor, action.draft.provider_id)
            if definition is None or self._blockers(actor, action):
                raise ToolCatalogError("Provider authorization changed before dispatch")
            dispatched = True
            receipt = self.providers.execute(definition, action)
            action = action.model_copy(
                update={
                    "status": receipt.status,
                    "provider_id": receipt.id,
                    "provider_status": receipt.provider_status,
                    "outcome": receipt.outcome,
                    "updated_at": utc_now(),
                }
            )
        except Exception as error:
            unknown = dispatched and (
                not isinstance(error, (ToolExecutionError, ToolCatalogError))
                or (isinstance(error, ToolExecutionError) and error.unknown)
            )
            action = action.model_copy(
                update={
                    "status": "unknown" if unknown else "failed",
                    "updated_at": utc_now(),
                    "error": (
                        "Provider outcome is unknown. Do not repeat this action; reconcile with "
                        "the provider first."
                        if unknown
                        else "The action was not completed. Review the provider configuration "
                        "and prepare a new request if appropriate."
                    ),
                }
            )
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, action.id)
            if self._action(job).status == "executing":
                self._save(actor, job, action)
            else:
                action = self._action(job)
        return action

    def refresh(self, actor: ActorContext, identifier: UUID) -> ExternalActionProposal:
        self._authorize(actor)
        action = self._action(self._job(actor, identifier))
        if action.status != "accepted":
            return action
        definition = self._definition(actor, action.draft.provider_id)
        if definition is None or self._blockers(actor, action):
            raise ToolCatalogError("Provider access changed; reconcile its receipt manually")
        receipt = self.providers.refresh(definition, action)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, identifier)
            current = self._action(job)
            if current.status != "accepted":
                return current
            updated = current.model_copy(
                update={
                    "status": receipt.status,
                    "provider_status": receipt.provider_status,
                    "outcome": receipt.outcome,
                    "updated_at": utc_now(),
                }
            )
            self._save(actor, job, updated)
            return updated

    def mark_interrupted_unknown(
        self, actor: ActorContext, identifier: UUID
    ) -> ExternalActionProposal:
        """Explicit operator reconciliation of stale claims; never executes a provider request."""
        self._authorize(actor, write=True, decision=True)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, identifier)
            action = self._action(job)
            if (
                action.status != "executing"
                or not action.claimed_at
                or utc_now() - action.claimed_at < timedelta(minutes=5)
            ):
                raise InvalidTransitionError(
                    "Only an interrupted claim older than five minutes qualifies"
                )
            action = action.model_copy(
                update={
                    "status": "unknown",
                    "updated_at": utc_now(),
                    "error": "Interrupted after claim; verify the provider outcome without retry.",
                }
            )
            self._save(actor, job, action)
            return action

    def reconcile(
        self,
        actor: ActorContext,
        identifier: UUID,
        body: ReconcileExternalAction,
        *,
        revalidate: Callable[[], ActorContext],
    ) -> ExternalActionProposal:
        """Record a user's provider investigation without sending another commitment."""
        self._authorize(actor, write=True, decision=True)
        body = ReconcileExternalAction.model_validate(body.model_dump())
        with self.store.transaction(actor.workspace_id):
            actor = self._revalidate(actor, revalidate)
            job = self._job(actor, identifier)
            action = self._action(job)
            if not hmac.compare_digest(body.review_digest, action.review_digest):
                raise InvalidTransitionError("Reload the exact action before reconciling it")
            if action.status != "unknown":
                raise InvalidTransitionError("Only an unknown provider outcome can be reconciled")
            now = utc_now()
            action = action.model_copy(
                update={
                    "status": "resolved",
                    "updated_at": now,
                    "outcome": "User reported that this action was "
                    + ("completed." if body.reported_outcome == "completed" else "not completed.")
                    + " This is a manual reconciliation, not provider confirmation.",
                    "manual_resolution": ManualActionResolution(
                        actor_id=actor.actor_id,
                        recorded_at=now,
                        reported_outcome=body.reported_outcome,
                        evidence=body.evidence,
                        note=body.note,
                    ),
                }
            )
            self._save(actor, job, action)
            return action
