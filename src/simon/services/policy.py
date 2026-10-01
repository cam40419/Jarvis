"""Generic capability authorization; specialized action review has its own service."""

from simon.domain.errors import AuthorizationError, ConfirmationRequiredError
from simon.domain.models import ActorContext, CapabilityDefinition, RiskClass


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
                raise ConfirmationRequiredError("a signed, action-bound confirmation is required")
            # The generic broker has no action-bound token verifier. Specialized
            # external-action services enforce their own review contracts.
            raise AuthorizationError("hard-write confirmation verification is not configured")
