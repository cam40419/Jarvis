from pydantic import ValidationError as PydanticValidationError

from jarvis.domain.errors import NotFoundError, ValidationError
from jarvis.domain.models import ActorContext, CapabilityInvocation, CapabilityResult
from jarvis.domain.ports import CapabilityStore, InvocationStore
from jarvis.services.audit import AuditService
from jarvis.services.canonical import digest
from jarvis.services.policy import PolicyEngine


class CapabilityBroker:
    def __init__(
        self,
        capabilities: CapabilityStore,
        invocations: InvocationStore,
        policy: PolicyEngine,
        audit: AuditService,
    ) -> None:
        self._capabilities = capabilities
        self._invocations = invocations
        self._policy = policy
        self._audit = audit

    def invoke(
        self, actor: ActorContext, invocation: CapabilityInvocation
    ) -> CapabilityResult:
        registration = self._capabilities.get(invocation.capability)
        if registration is None:
            raise NotFoundError(f"unknown capability: {invocation.capability}")
        definition, input_model, handler = registration
        self._policy.authorize(actor, definition, invocation.confirmation_token)

        request_digest = digest(
            {
                "capability": invocation.capability,
                "arguments": invocation.arguments,
                "actor_id": str(actor.actor_id),
                "household_id": str(actor.household_id),
            }
        )
        namespace = f"{actor.household_id}:{invocation.capability}"
        try:
            validated = input_model.model_validate(invocation.arguments)
        except PydanticValidationError as exc:
            raise ValidationError(str(exc)) from exc

        output, replayed = self._invocations.execute_once(
            namespace,
            invocation.idempotency_key,
            request_digest,
            lambda: handler(validated),
        )
        result = CapabilityResult(
            capability=invocation.capability, output=output, replayed=replayed
        )
        if replayed:
            return result
        self._audit.record(
            event_type="capability.invoked",
            actor=actor,
            resource_type="capability",
            resource_id=invocation.capability,
            payload={"invocation_id": str(result.invocation_id), "risk": definition.risk.value},
        )
        return result
