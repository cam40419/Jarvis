from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from jarvis.adapters.memory import InMemoryStore
from jarvis.domain.errors import (
    AuthorizationError,
    ConfirmationRequiredError,
    IdempotencyConflictError,
    ValidationError,
)
from jarvis.domain.models import (
    ActorContext,
    CapabilityDefinition,
    CapabilityInvocation,
    Channel,
    RiskClass,
)
from jarvis.services.audit import AuditService
from jarvis.services.capabilities import CapabilityBroker
from jarvis.services.policy import PolicyEngine


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def actor(*scopes: str) -> ActorContext:
    return ActorContext(
        actor_id=uuid4(), household_id=uuid4(), channel=Channel.CHAT, scopes=frozenset(scopes)
    )


def broker(risk: RiskClass = RiskClass.READ) -> tuple[CapabilityBroker, InMemoryStore]:
    store = InMemoryStore()
    store.register(
        CapabilityDefinition(
            name="test.double",
            description="Double a number.",
            risk=risk,
            required_scopes=frozenset({"test:run"}),
            input_schema=Input.model_json_schema(),
            output_schema={"type": "object"},
        ),
        Input,
        lambda item: {"value": item.value * 2},
    )
    return CapabilityBroker(store, store, PolicyEngine(), AuditService(store)), store


def test_invocation_is_validated_idempotent_and_audited() -> None:
    service, store = broker()
    context = actor("test:run")
    invocation = CapabilityInvocation(
        capability="test.double", arguments={"value": 4}, idempotency_key="request-123"
    )

    first = service.invoke(context, invocation)
    replay = service.invoke(context, invocation)

    assert first.output == {"value": 8}
    assert first.replayed is False
    assert replay.output == first.output
    assert replay.replayed is True
    assert len(store.audit_events()) == 1


def test_idempotency_key_cannot_be_reused_for_different_input() -> None:
    service, _ = broker()
    context = actor("test:run")
    service.invoke(
        context,
        CapabilityInvocation(
            capability="test.double", arguments={"value": 4}, idempotency_key="request-123"
        ),
    )

    with pytest.raises(IdempotencyConflictError):
        service.invoke(
            context,
            CapabilityInvocation(
                capability="test.double", arguments={"value": 5}, idempotency_key="request-123"
            ),
        )


def test_missing_scope_is_denied() -> None:
    service, _ = broker()
    with pytest.raises(AuthorizationError):
        service.invoke(
            actor(),
            CapabilityInvocation(
                capability="test.double", arguments={"value": 4}, idempotency_key="request-123"
            ),
        )


def test_extra_input_is_rejected() -> None:
    service, _ = broker()
    with pytest.raises(ValidationError):
        service.invoke(
            actor("test:run"),
            CapabilityInvocation(
                capability="test.double",
                arguments={"value": 4, "surprise": True},
                idempotency_key="request-123",
            ),
        )


def test_hard_write_requires_confirmation_then_still_fails_closed() -> None:
    service, _ = broker(RiskClass.WRITE_HARD)
    invocation = CapabilityInvocation(
        capability="test.double", arguments={"value": 4}, idempotency_key="request-123"
    )
    with pytest.raises(ConfirmationRequiredError):
        service.invoke(actor("test:run"), invocation)

    with pytest.raises(AuthorizationError):
        service.invoke(
            actor("test:run"), invocation.model_copy(update={"confirmation_token": "unverified"})
        )


def test_concurrent_retries_execute_handler_only_once() -> None:
    store = InMemoryStore()
    call_count = 0
    count_lock = Lock()

    def slow_handler(item: BaseModel) -> dict[str, int]:
        nonlocal call_count
        Input.model_validate(item)
        sleep(0.01)
        with count_lock:
            call_count += 1
        return {"value": 8}

    store.register(
        CapabilityDefinition(
            name="test.concurrent",
            description="Exercise atomic idempotency.",
            risk=RiskClass.READ,
            required_scopes=frozenset({"test:run"}),
            input_schema=Input.model_json_schema(),
            output_schema={"type": "object"},
        ),
        Input,
        slow_handler,
    )
    service = CapabilityBroker(store, store, PolicyEngine(), AuditService(store))
    context = actor("test:run")
    invocation = CapabilityInvocation(
        capability="test.concurrent",
        arguments={"value": 4},
        idempotency_key="concurrent-request",
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: service.invoke(context, invocation), range(8)))

    assert call_count == 1
    assert sum(not result.replayed for result in results) == 1
    assert len(store.audit_events()) == 1
