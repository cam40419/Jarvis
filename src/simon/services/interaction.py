from uuid import UUID

from simon.domain.errors import InvalidTransitionError
from simon.domain.interaction import (
    AnswerReference,
    ResponsePreferences,
    RunFeedback,
    SaveFeedback,
    SavePreferences,
)
from simon.domain.models import ActorContext
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.conversations import ConversationService


class InteractionService:
    """Personal settings and feedback within the active household; no model side effects."""

    def __init__(self, store: Store, audit: AuditService) -> None:
        self.store = store
        self.audit = audit
        self.conversations = ConversationService(store, audit)

    def preferences(self, actor: ActorContext) -> ResponsePreferences:
        self.conversations.authorize(actor, "threads:read")
        return (
            self.store.response_preferences(actor.household_id, actor.actor_id)
            or ResponsePreferences()
        )

    def save_preferences(
        self, actor: ActorContext, request: SavePreferences
    ) -> ResponsePreferences:
        self.conversations.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):

            def operation() -> dict[str, object]:
                current = self.preferences(actor)
                if current.version != request.expected_version:
                    raise InvalidTransitionError(
                        "Preferences changed elsewhere. Reload before saving."
                    )
                preferences = ResponsePreferences(
                    profile=request.profile,
                    answer_length=request.answer_length,
                    auto_deep_enabled=request.auto_deep_enabled,
                    version=current.version + 1,
                )
                self.store.save_response_preferences(
                    actor.household_id, actor.actor_id, preferences
                )
                self.audit.record(
                    event_type="response_preferences.updated",
                    actor=actor,
                    resource_type="response_preferences",
                    resource_id=str(actor.actor_id),
                    payload=preferences.model_dump(mode="json"),
                )
                return preferences.model_dump(mode="json")

            self.store.execute_once(
                f"preferences:{actor.household_id}:{actor.actor_id}",
                request.idempotency_key,
                digest(request.model_dump(mode="json", exclude={"idempotency_key"})),
                operation,
            )
            # A delayed retry never replaces a newer saved preference.
            return self.preferences(actor)

    def answers(
        self, actor: ActorContext, thread_id: UUID, offset: int = 0, limit: int = 100
    ) -> tuple[AnswerReference, ...]:
        with self.store.transaction(actor.household_id):
            self.conversations.get(actor, thread_id)
            result = []
            for run in self.store.answer_runs(thread_id, offset, limit):
                if run.memory_context:
                    self.conversations.authorize(actor, "memories:read")
                result.append(
                    AnswerReference(
                        run_id=run.id,
                        output_message_id=run.output_message_id,
                        feedback=self.store.feedback(actor.household_id, actor.actor_id, run.id),
                        web_sources=run.web_sources,
                        home_commands=tuple(
                            self.store.home_commands(actor.household_id, actor.actor_id, run.id)
                        )
                        if "home:read" in actor.scopes
                        else (),
                        actions=tuple(
                            action
                            for action_id in run.action_ids
                            if (action := self.store.action(action_id))
                            and action.actor_id == actor.actor_id
                            and action.household_id == actor.household_id
                        ),
                    )
                )
            return tuple(result)

    def save_feedback(
        self, actor: ActorContext, run_id: UUID, request: SaveFeedback
    ) -> RunFeedback:
        self.conversations.authorize(actor, "threads:write")
        with self.store.transaction(actor.household_id):
            self.conversations.run(actor, run_id)

            def operation() -> dict[str, object]:
                current = self.store.feedback(actor.household_id, actor.actor_id, run_id)
                version = current.version if current else 0
                if version != request.expected_version:
                    raise InvalidTransitionError(
                        "Feedback changed elsewhere. Reload before saving."
                    )
                feedback = RunFeedback(run_id=run_id, rating=request.rating, version=version + 1)
                self.store.save_feedback(actor.household_id, actor.actor_id, feedback)
                self.audit.record(
                    event_type="run.feedback_updated",
                    actor=actor,
                    resource_type="run",
                    resource_id=str(run_id),
                    payload={"rating": request.rating, "version": feedback.version},
                )
                return feedback.model_dump(mode="json")

            self.store.execute_once(
                f"feedback:{actor.household_id}:{actor.actor_id}:{run_id}",
                request.idempotency_key,
                digest(request.model_dump(mode="json", exclude={"idempotency_key"})),
                operation,
            )
            saved = self.store.feedback(actor.household_id, actor.actor_id, run_id)
            assert saved is not None
            return saved
