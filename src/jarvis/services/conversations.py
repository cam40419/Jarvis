from uuid import UUID

from jarvis.domain.conversations import (
    CreateThread,
    Message,
    Run,
    RunEvent,
    SubmitRun,
    Thread,
)
from jarvis.domain.errors import (
    AuthorizationError,
    ModelBusyError,
    ModelError,
    NotFoundError,
    ValidationError,
)
from jarvis.domain.models import ActorContext
from jarvis.domain.ports import Store
from jarvis.services.audit import AuditService
from jarvis.services.canonical import digest
from jarvis.services.context import ContextAssembler


class ConversationService:
    def __init__(self, store: Store, audit: AuditService) -> None:
        self.store = store
        self.audit = audit
        self.context = ContextAssembler()

    @staticmethod
    def authorize(actor: ActorContext, scope: str) -> None:
        if scope not in actor.scopes:
            raise AuthorizationError(f"missing required scopes: {scope}")

    def create(self, actor: ActorContext, request: CreateThread) -> Thread:
        self.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):

            def operation() -> dict[str, object]:
                thread = Thread(
                    household_id=actor.household_id, created_by=actor.actor_id, title=request.title
                )
                self.store.insert_thread(thread)
                self.audit.record(
                    event_type="thread.created",
                    actor=actor,
                    resource_type="thread",
                    resource_id=str(thread.id),
                    payload={},
                )
                return thread.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"thread:{actor.household_id}:{actor.actor_id}",
                request.idempotency_key,
                digest(request.title),
                operation,
            )
            return Thread.model_validate(result)

    def get(self, actor: ActorContext, thread_id: UUID) -> Thread:
        self.authorize(actor, "threads:read")
        thread = self.store.thread(actor.household_id, thread_id)
        if thread is None:
            raise NotFoundError("thread not found")
        return thread

    def list(self, actor: ActorContext, offset: int, limit: int) -> tuple[Thread, ...]:
        self.authorize(actor, "threads:read")
        return tuple(self.store.threads(actor.household_id, offset, limit))

    def messages(
        self, actor: ActorContext, thread_id: UUID, after: int, limit: int
    ) -> tuple[Message, ...]:
        self.get(actor, thread_id)
        return tuple(self.store.messages(thread_id, after, limit))

    def submit(self, actor: ActorContext, thread_id: UUID, request: SubmitRun) -> Run:
        self.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):
            self.get(actor, thread_id)

            def operation() -> dict[str, object]:
                history = self.store.recent_messages(thread_id, 32)
                user = Message(
                    thread_id=thread_id,
                    sequence=history[-1].sequence + 1 if history else 1,
                    role="user",
                    text=request.text,
                )
                reply = Message(
                    thread_id=thread_id,
                    sequence=user.sequence + 1,
                    role="assistant",
                    text=f"Test runner received: {request.text}",
                )
                memories = (
                    self.store.explicit_memories(actor.household_id, 0, 100)
                    if "memories:read" in actor.scopes
                    else ()
                )
                context, memory_context, summary, policy = self.context.assemble(
                    history, user, memories
                )
                run = Run(
                    thread_id=thread_id,
                    actor_id=actor.actor_id,
                    input_message_id=user.id,
                    output_message_id=reply.id,
                    context=context,
                    memory_context=memory_context,
                    summary_context=summary,
                    context_policy=policy,
                )
                events = (
                    RunEvent(run_id=run.id, sequence=1, event_type="run.started"),
                    RunEvent(
                        run_id=run.id, sequence=2, event_type="message.created", message_id=user.id
                    ),
                    RunEvent(
                        run_id=run.id, sequence=3, event_type="message.created", message_id=reply.id
                    ),
                    RunEvent(run_id=run.id, sequence=4, event_type="run.completed"),
                )
                self.store.insert_message(user)
                self.store.insert_message(reply)
                self.store.insert_run(run, events)
                self.audit.record(
                    event_type="run.completed",
                    actor=actor.model_copy(update={"correlation_id": run.correlation_id}),
                    resource_type="run",
                    resource_id=str(run.id),
                    payload={"thread_id": str(thread_id)},
                )
                return run.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"run:{actor.household_id}:{actor.actor_id}:{thread_id}",
                request.idempotency_key,
                digest(request.text),
                operation,
            )
            if "attempt_id" in result:
                attempt = self.store.attempt(UUID(str(result["attempt_id"])))
                assert attempt is not None
                if attempt.status == "succeeded":
                    return self.run(actor, attempt.run.id)
                if attempt.status == "failed":
                    raise ModelError(attempt.error_code or "model_unavailable")
                raise ModelBusyError("This model request is still processing.")
            run = Run.model_validate(result)
            if run.memory_context:
                self.authorize(actor, "memories:read")
            return run

    def run(self, actor: ActorContext, run_id: UUID) -> Run:
        self.authorize(actor, "threads:read")
        run = self.store.run(run_id)
        if run is None:
            raise NotFoundError("run not found")
        self.get(actor, run.thread_id)
        if run.memory_context:
            self.authorize(actor, "memories:read")
        return run

    def events(self, actor: ActorContext, run_id: UUID, after: int) -> tuple[RunEvent, ...]:
        self.run(actor, run_id)
        events = self.store.run_events(run_id)
        if after < 0 or after > len(events):
            raise ValidationError("invalid event cursor")
        return tuple(event for event in events if event.sequence > after)
