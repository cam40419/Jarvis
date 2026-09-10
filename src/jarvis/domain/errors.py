class DomainError(Exception):
    """Base error safe to translate at an API boundary."""

    code = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class AuthorizationError(DomainError):
    code = "forbidden"


class ConfirmationRequiredError(DomainError):
    code = "confirmation_required"


class IdempotencyConflictError(DomainError):
    code = "idempotency_conflict"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class ValidationError(DomainError):
    code = "validation_error"

