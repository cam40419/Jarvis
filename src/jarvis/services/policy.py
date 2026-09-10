from jarvis.domain.errors import AuthorizationError, ConfirmationRequiredError
from jarvis.domain.models import ActorContext, CapabilityDefinition, RiskClass


class PolicyEngine:
    """Deterministic, deny-by-default capability policy."""

    def authorize(
        self,
        actor: ActorContext,
        capability: CapabilityDefinition,
        confirmation_token: str | None,
    ) -> None:
        missing = capability.required_scopes - actor.scopes
        if missing:
            raise AuthorizationError(f"missing required scopes: {', '.join(sorted(missing))}")

        if capability.risk in {RiskClass.WRITE_HARD, RiskClass.DANGEROUS}:
            if not confirmation_token:
                raise ConfirmationRequiredError(
                    "a signed, action-bound confirmation is required"
                )
            # Token verification will live behind a ConfirmationVerifier port.
            # Until it exists, hard actions are intentionally impossible.
            raise AuthorizationError("hard-write confirmation verification is not configured")

