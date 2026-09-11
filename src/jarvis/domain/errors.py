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


class AuthenticationError(DomainError):
    code = "unauthenticated"


MODEL_ERRORS = {
    "model_unavailable": "The assistant could not complete this request. Please try a new request.",
    "model_credentials": "OpenAI rejected the API key. Check the server configuration.",
    "model_rate_limit": "OpenAI quota or rate limit reached. Check API billing or try again later.",
    "model_timeout": "The model request timed out. Its provider outcome is unknown.",
    "model_input_limit": "Request exceeds the model input limit. Start a shorter conversation.",
    "model_incomplete": "The model did not return a complete answer. Try a shorter question.",
    "model_access_changed": "Access changed while the answer was being generated. Sign in again.",
}


class ModelError(DomainError):
    code = "model_error"

    def __init__(self, reason: str = "model_unavailable") -> None:
        self.reason = reason if reason in MODEL_ERRORS else "model_unavailable"
        super().__init__(MODEL_ERRORS[self.reason])


class ModelBusyError(DomainError):
    code = "model_busy"
