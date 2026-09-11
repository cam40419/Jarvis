from uuid import UUID

from jarvis.domain.context import CreateMemory, ExplicitMemory
from jarvis.domain.errors import AuthorizationError, NotFoundError, ValidationError
from jarvis.domain.models import ActorContext
from jarvis.domain.ports import Store
from jarvis.services.audit import AuditService
from jarvis.services.canonical import digest


class MemoryService:
    def __init__(self, store: Store, audit: AuditService) -> None:
        self.store = store
        self.audit = audit

    @staticmethod
    def authorize(actor: ActorContext, scope: str) -> None:
        if scope not in actor.scopes:
            raise AuthorizationError(f"missing required scopes: {scope}")

    def list(
        self, actor: ActorContext, offset: int = 0, limit: int = 100
    ) -> tuple[ExplicitMemory, ...]:
        self.authorize(actor, "memories:read")
        return tuple(self.store.explicit_memories(actor.household_id, offset, limit))

    def create(self, actor: ActorContext, request: CreateMemory) -> ExplicitMemory:
        self.authorize(actor, "memories:write")
        with self.store.transaction(actor.household_id):

            def operation() -> dict[str, object]:
                if len(self.store.explicit_memories(actor.household_id, 0, 100)) >= 100:
                    raise ValidationError("households are limited to 100 active explicit memories")
                memory = ExplicitMemory(
                    household_id=actor.household_id,
                    created_by=actor.actor_id,
                    subject=request.subject,
                    content=request.content,
                )
                self.store.insert_memory(memory)
                self.audit.record(
                    event_type="memory.accepted",
                    actor=actor,
                    resource_type="memory",
                    resource_id=str(memory.id),
                    payload={},
                )
                return memory.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"memory:{actor.household_id}:{actor.actor_id}",
                request.idempotency_key,
                digest({"subject": request.subject, "content": request.content}),
                operation,
            )
            # A retry must not make a subsequently retracted memory look active again.
            memory = self.store.explicit_memory(actor.household_id, UUID(str(result["id"])))
            assert memory is not None
            return memory

    def retract(self, actor: ActorContext, memory_id: UUID) -> ExplicitMemory:
        self.authorize(actor, "memories:write")
        with self.store.transaction(actor.household_id):
            memory = self.store.explicit_memory(actor.household_id, memory_id)
            if memory is None:
                raise NotFoundError("memory not found")
            if memory.created_by != actor.actor_id and "memories:manage" not in actor.scopes:
                raise AuthorizationError("only the creator or a household owner can retract memory")
            if memory.accepted:
                self.store.retract_memory(memory)
                self.audit.record(
                    event_type="memory.retracted",
                    actor=actor,
                    resource_type="memory",
                    resource_id=str(memory.id),
                    payload={},
                )
            return memory.model_copy(update={"accepted": False})
