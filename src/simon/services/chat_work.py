"""Identity checks and durable jobs for background chat conversations."""

from contextlib import suppress
from typing import Any
from uuid import UUID

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext, Channel, Job, utc_now
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES, IdentityService
from simon.services.jobs import JobService
from simon.services.model_conversations import ModelConversationService


class ChatWorkService:
    def __init__(
        self,
        store: Store,
        identity: IdentityService,
        conversations: ModelConversationService | None = None,
    ) -> None:
        self.store, self.identity, self.conversations = store, identity, conversations
        self.audit = AuditService(store)
        self.jobs = JobService(store, self.audit)

    @staticmethod
    def authorize(actor: ActorContext, *, write: bool = False) -> None:
        scope = "jobs:write" if write else "jobs:read"
        if scope not in actor.scopes:
            raise AuthorizationError(f"missing required scopes: {scope}")

    def _save(self, job: Job, expected: int, **updates: Any) -> Job:
        input_data = updates.pop("input", job.input)
        updated = job.model_copy(
            update={
                **updates,
                "input": input_data,
                "input_digest": digest(input_data),
                "updated_at": utc_now(),
            }
        )
        return self.store.save_job(updated, expected)

    def _cancel_model(self, actor: ActorContext, job: Job) -> None:
        if self.conversations and job.input.get("run_id"):
            with suppress(Exception):
                self.conversations.cancel(actor, UUID(job.input["run_id"]))

    def worker_actor(self, job: Job) -> ActorContext:
        member = self.identity.membership(job.created_by, job.workspace_id)
        scopes = ROLE_SCOPES[member.role]
        if not {"jobs:write", "threads:write", "threads:read"} <= scopes:
            raise AuthorizationError("conversation access changed")
        return ActorContext(
            actor_id=job.created_by,
            workspace_id=job.workspace_id,
            channel=Channel.WORKER,
            scopes=scopes,
        )
