from uuid import uuid4

from jarvis.adapters.memory import InMemoryStore
from jarvis.domain.models import ActorContext, Channel
from jarvis.services.audit import AuditService


def test_audit_events_form_a_hash_chain() -> None:
    store = InMemoryStore()
    service = AuditService(store)
    actor = ActorContext(actor_id=uuid4(), household_id=uuid4(), channel=Channel.API)
    first = service.record(
        event_type="test.one",
        actor=actor,
        resource_type="test",
        resource_id="one",
        payload={"safe": True},
    )
    second = service.record(
        event_type="test.two",
        actor=actor,
        resource_type="test",
        resource_id="two",
        payload={"safe": True},
    )
    assert first.previous_hash == "0" * 64
    assert second.previous_hash == first.event_hash
    assert second.sequence == 2

