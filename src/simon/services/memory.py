from uuid import UUID

from simon.domain.context import CreateMemory, ExplicitMemory, RememberFact
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.models import ActorContext
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.canonical import digest


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
        return tuple(
            self.store.explicit_memories(actor.household_id, offset, limit, actor.actor_id)
        )

    def create(self, actor: ActorContext, request: CreateMemory) -> ExplicitMemory:
        self.authorize(actor, "memories:write")
        with self.store.transaction(actor.household_id):

            def operation() -> dict[str, object]:
                if (
                    request.scope == "household"
                    and len(
                        self.store.explicit_memories(actor.household_id, 0, 100, personal=False)
                    )
                    >= 100
                ):
                    raise ValidationError("households are limited to 100 active explicit memories")
                if (
                    len(self.store.explicit_memories(actor.household_id, 0, 500, actor.actor_id))
                    >= 500
                ):
                    raise ValidationError("limited to 500 visible active memories")
                memory = ExplicitMemory(
                    household_id=actor.household_id,
                    created_by=actor.actor_id,
                    subject=request.subject,
                    content=request.content,
                    scope=request.scope,
                    category=request.category,
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
                digest(
                    {"subject": request.subject, "content": request.content}
                    | (
                        {"scope": request.scope, "category": request.category}
                        if request.scope != "household" or request.category != "fact"
                        else {}
                    )
                ),
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
            if memory.scope == "personal" and memory.created_by != actor.actor_id:
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

    def remember(self, actor: ActorContext, run_id: UUID, fact: RememberFact) -> ExplicitMemory:
        """Save only from the current user's persisted request, never from tool/web output."""
        self.authorize(actor, "memories:write")
        with self.store.transaction(actor.household_id):
            attempt = self.store.attempt(run_id)
            if (
                not attempt
                or attempt.household_id != actor.household_id
                or attempt.run.actor_id != actor.actor_id
                or attempt.status != "pending"
            ):
                raise NotFoundError("active request not found")
            source = attempt.user.text
            if source.startswith("Voice request (transcription may contain mistakes)."):
                source = source.partition("\nLatest user request:\n")[2]
            if " ".join(fact.evidence.split()) not in " ".join(source.split()):
                raise ValidationError("Evidence must quote the current user's own statement")
            key = "personal:" + str(run_id) + ":" + digest(fact.model_dump())

            def operation() -> dict[str, object]:
                visible = self.list(actor, 0, 500)
                prior = next(
                    (
                        m
                        for m in visible
                        if m.scope == "personal"
                        and m.subject.strip().casefold() == fact.subject.strip().casefold()
                    ),
                    None,
                )
                if prior and prior.content == fact.content and prior.category == fact.category:
                    return {"id": str(prior.id)}
                if len(visible) >= 500 and not prior:
                    raise ValidationError("limited to 500 visible active memories")
                memory = ExplicitMemory(
                    household_id=actor.household_id,
                    created_by=actor.actor_id,
                    scope="personal",
                    subject=fact.subject.strip(),
                    content=fact.content,
                    category=fact.category,
                    source_message_id=attempt.user.id,
                    supersedes=prior.id if prior else None,
                )
                if prior:
                    self.retract(actor, prior.id)
                self.store.insert_memory(memory)
                self.audit.record(
                    event_type="memory.accepted",
                    actor=actor,
                    resource_type="memory",
                    resource_id=str(memory.id),
                    payload={},
                )
                return {"id": str(memory.id)}

            result, _ = self.store.execute_once(
                f"memory:{actor.household_id}:{actor.actor_id}",
                key,
                digest(fact.model_dump()),
                operation,
            )
            memory = self.store.explicit_memory(actor.household_id, UUID(str(result["id"])))
            assert memory is not None
            return memory
