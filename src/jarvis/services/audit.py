from jarvis.domain.models import ActorContext, AuditEvent, OutboxEvent
from jarvis.domain.ports import Store
from jarvis.services.canonical import digest


class AuditService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        *,
        event_type: str,
        actor: ActorContext,
        resource_type: str,
        resource_id: str,
        payload: dict[str, object],
    ) -> AuditEvent:
        with self._store.transaction(actor.household_id):
            events = self._store.audit_events(actor.household_id)
            sequence = len(events) + 1
            previous_hash = events[-1].event_hash if events else "0" * 64
            event_hash = digest(
                {
                    "sequence": sequence,
                    "event_type": event_type,
                    "actor_id": str(actor.actor_id),
                    "household_id": str(actor.household_id),
                    "correlation_id": str(actor.correlation_id),
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "payload": payload,
                    "previous_hash": previous_hash,
                }
            )
            event = AuditEvent(
                sequence=sequence,
                event_type=event_type,
                actor_id=actor.actor_id,
                household_id=actor.household_id,
                correlation_id=actor.correlation_id,
                resource_type=resource_type,
                resource_id=resource_id,
                payload=payload,
                previous_hash=previous_hash,
                event_hash=event_hash,
            )
            self._store.append_audit(event)
            self._store.add_outbox(
                OutboxEvent(
                    id=event.id,
                    aggregate_type=resource_type,
                    aggregate_id=resource_id,
                    event_type=event_type,
                    payload={"household_id": str(actor.household_id), **payload},
                    correlation_id=actor.correlation_id,
                    causation_id=event.id,
                )
            )
            return event
