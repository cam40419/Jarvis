from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from jarvis.adapters.memory import InMemoryStore
from jarvis.domain.context import ExplicitMemory
from jarvis.domain.conversations import Message, ModelAttempt, Run, RunEvent, Thread
from jarvis.domain.errors import (
    AuthenticationError,
    IdempotencyConflictError,
    InvalidTransitionError,
)
from jarvis.domain.identity import Challenge, Enrollment, Membership, Passkey, Session
from jarvis.domain.models import AuditEvent, Job, JobStatus, OutboxEvent


class PostgresStore(InMemoryStore):
    """Durable state with a process-local registry of executable capability handlers.

    Synchronous request threads get independent connections. Nested service/store calls
    share one transaction, using savepoints. Household locks serialize audit sequencing.
    External side effects are not covered by the database transaction.
    """

    def __init__(self, database_url: str) -> None:
        super().__init__()
        self._database_url = database_url
        self._connection: ContextVar[psycopg.Connection[dict[str, Any]] | None] = ContextVar(
            "jarvis_connection", default=None
        )

    @contextmanager
    def transaction(self, household_id: UUID | None = None) -> Iterator[None]:
        current = self._connection.get()
        if current is not None:
            with current.transaction():
                if household_id is not None:
                    self._advisory_lock(f"household:{household_id}")
                yield
            return
        with psycopg.connect(
            self._database_url, row_factory=dict_row, autocommit=True, connect_timeout=5
        ) as connection:
            token = self._connection.set(connection)
            try:
                with connection.transaction():
                    connection.execute("SET LOCAL lock_timeout = '10s'")
                    connection.execute("SET LOCAL statement_timeout = '30s'")
                    if household_id is not None:
                        self._advisory_lock(f"household:{household_id}")
                    yield
            finally:
                self._connection.reset(token)

    @property
    def connection(self) -> psycopg.Connection[dict[str, Any]]:
        connection = self._connection.get()
        if connection is None:
            raise RuntimeError("database operation requires a transaction")
        return connection

    def _advisory_lock(self, key: str) -> None:
        self.connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    def execute_once(
        self,
        namespace: str,
        key: str,
        request_digest: str,
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        with self.transaction():
            self._advisory_lock(f"invocation:{namespace}:{key}")
            row = self.connection.execute(
                "SELECT request_digest, response FROM idempotency_records "
                "WHERE namespace = %s AND idempotency_key = %s",
                (namespace, key),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise IdempotencyConflictError("conflicting invocation")
                return dict(row["response"]), True
            output = operation()
            self.connection.execute(
                "INSERT INTO idempotency_records "
                "(namespace, idempotency_key, request_digest, status, response) "
                "VALUES (%s, %s, %s, 'completed', %s)",
                (namespace, key, request_digest, Jsonb(output)),
            )
            return output, False

    def save_attempt(self, attempt: ModelAttempt) -> None:
        with self.transaction(attempt.household_id):
            self.connection.execute(
                "INSERT INTO model_attempts (id, household_id, thread_id, status, snapshot) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE "
                "SET status = EXCLUDED.status, snapshot = EXCLUDED.snapshot",
                (
                    attempt.run.id,
                    attempt.household_id,
                    attempt.run.thread_id,
                    attempt.status,
                    Jsonb(attempt.model_dump(mode="json")),
                ),
            )

    def attempt(self, run_id: UUID) -> ModelAttempt | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM model_attempts WHERE id = %s", (run_id,)
            ).fetchone()
            return ModelAttempt.model_validate(row["snapshot"]) if row else None

    def pending_attempt(self, thread_id: UUID) -> ModelAttempt | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM model_attempts WHERE thread_id = %s AND status = 'pending'",
                (thread_id,),
            ).fetchone()
            return ModelAttempt.model_validate(row["snapshot"]) if row else None

    def recent_messages(self, thread_id: UUID, limit: int) -> tuple[Message, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id, thread_id, sequence, role, content->>'text' AS text, created_at "
                "FROM messages WHERE thread_id = %s ORDER BY sequence DESC LIMIT %s",
                (thread_id, limit),
            ).fetchall()
            return tuple(Message.model_validate(row) for row in reversed(rows))

    def insert_memory(self, memory: ExplicitMemory) -> None:
        with self.transaction(memory.household_id):
            self.connection.execute(
                "INSERT INTO memories (id, household_id, subject, scope, kind, content, source, "
                "confidence, sensitivity, review_status, created_at, explicit_snapshot) "
                "VALUES (%s,%s,%s,'household','explicit',%s,%s,1,'personal','accepted',%s,%s)",
                (
                    memory.id,
                    memory.household_id,
                    memory.subject,
                    memory.content,
                    Jsonb({"type": memory.source, "actor_id": str(memory.created_by)}),
                    memory.created_at,
                    Jsonb(memory.model_dump(mode="json")),
                ),
            )

    def explicit_memories(
        self, household_id: UUID, offset: int, limit: int
    ) -> tuple[ExplicitMemory, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT explicit_snapshot FROM memories WHERE household_id = %s "
                "AND explicit_snapshot IS NOT NULL AND review_status = 'accepted' "
                "ORDER BY created_at, id LIMIT %s OFFSET %s",
                (household_id, limit, offset),
            ).fetchall()
            return tuple(ExplicitMemory.model_validate(row["explicit_snapshot"]) for row in rows)

    def explicit_memory(self, household_id: UUID, memory_id: UUID) -> ExplicitMemory | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT explicit_snapshot FROM memories WHERE household_id = %s AND id = %s "
                "AND explicit_snapshot IS NOT NULL",
                (household_id, memory_id),
            ).fetchone()
            return ExplicitMemory.model_validate(row["explicit_snapshot"]) if row else None

    def retract_memory(self, memory: ExplicitMemory) -> None:
        with self.transaction(memory.household_id):
            self.connection.execute(
                "UPDATE memories SET review_status = 'rejected', explicit_snapshot = %s "
                "WHERE household_id = %s AND id = %s",
                (
                    Jsonb(memory.model_copy(update={"accepted": False}).model_dump(mode="json")),
                    memory.household_id,
                    memory.id,
                ),
            )

    def insert_thread(self, thread: Thread) -> None:
        with self.transaction(thread.household_id):
            self.connection.execute(
                "INSERT INTO threads (id, household_id, created_by, title, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    thread.id,
                    thread.household_id,
                    thread.created_by,
                    thread.title,
                    thread.created_at,
                ),
            )

    def threads(self, household_id: UUID, offset: int, limit: int) -> tuple[Thread, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id, household_id, created_by, title, created_at FROM threads "
                "WHERE household_id = %s ORDER BY created_at, id LIMIT %s OFFSET %s",
                (household_id, limit, offset),
            ).fetchall()
            return tuple(Thread.model_validate(row) for row in rows)

    def thread(self, household_id: UUID, thread_id: UUID) -> Thread | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT id, household_id, created_by, title, created_at FROM threads "
                "WHERE household_id = %s AND id = %s",
                (household_id, thread_id),
            ).fetchone()
            return Thread.model_validate(row) if row else None

    def messages(self, thread_id: UUID, after: int, limit: int) -> tuple[Message, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id, thread_id, sequence, role, content->>'text' AS text, created_at "
                "FROM messages WHERE thread_id = %s AND sequence > %s "
                "ORDER BY sequence LIMIT %s",
                (thread_id, after, limit),
            ).fetchall()
            return tuple(Message.model_validate(row) for row in rows)

    def insert_message(self, message: Message) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO messages "
                "(id, thread_id, sequence, role, content, channel, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'api', %s)",
                (
                    message.id,
                    message.thread_id,
                    message.sequence,
                    message.role,
                    Jsonb({"text": message.text}),
                    message.created_at,
                ),
            )

    def insert_run(self, run: Run, events: tuple[RunEvent, ...]) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO runs (id, thread_id, actor_id, correlation_id, model_provider, "
                "model_name, prompt_release, capability_manifest, status, created_at, "
                "completed_at, snapshot) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run.id,
                    run.thread_id,
                    run.actor_id,
                    run.correlation_id,
                    run.model_provider,
                    run.model_name,
                    run.prompt_release,
                    Jsonb(list(run.capability_manifest)),
                    run.status,
                    run.created_at,
                    run.completed_at,
                    Jsonb(run.model_dump(mode="json")),
                ),
            )
            for event in events:
                self.connection.execute(
                    "INSERT INTO run_events (run_id, sequence, event_type, message_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (event.run_id, event.sequence, event.event_type, event.message_id),
                )

    def run(self, run_id: UUID) -> Run | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM runs WHERE id = %s",
                (run_id,),
            ).fetchone()
            return Run.model_validate(row["snapshot"]) if row and row["snapshot"] else None

    def run_events(self, run_id: UUID) -> tuple[RunEvent, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT run_id, sequence, event_type, message_id FROM run_events "
                "WHERE run_id = %s ORDER BY sequence",
                (run_id,),
            ).fetchall()
            return tuple(RunEvent.model_validate(row) for row in rows)

    def create_job(self, job: Job) -> tuple[Job, bool]:
        with self.transaction(job.household_id):
            row = self.connection.execute(
                "INSERT INTO jobs (id, household_id, created_by, kind, schema_version, "
                "idempotency_key, input, input_digest, status, version, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (household_id, kind, idempotency_key) DO NOTHING RETURNING *",
                (
                    job.id,
                    job.household_id,
                    job.created_by,
                    job.kind,
                    job.schema_version,
                    job.idempotency_key,
                    Jsonb(job.input),
                    job.input_digest,
                    job.status.value,
                    job.version,
                    job.created_at,
                    job.updated_at,
                ),
            ).fetchone()
            if row is not None:
                return self._job(row), True
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE household_id = %s AND kind = %s AND idempotency_key = %s",
                (job.household_id, job.kind, job.idempotency_key),
            ).fetchone()
            assert row is not None
            if row["input_digest"] != job.input_digest or row["created_by"] != job.created_by:
                raise IdempotencyConflictError("conflicting job submission")
            return self._job(row), False

    @staticmethod
    def _job(row: dict[str, Any]) -> Job:
        return Job.model_validate(
            {key: value for key, value in row.items() if key in Job.model_fields}
        )

    def get_job(self, job_id: UUID) -> Job | None:
        with self.transaction():
            row = self.connection.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
            return self._job(row) if row is not None else None

    def transition_job(
        self,
        job_id: UUID,
        expected_version: int,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job:
        with self.transaction():
            row = self.connection.execute(
                "UPDATE jobs SET status = %s, version = version + 1, updated_at = now(), "
                "result = %s, error_code = %s WHERE id = %s AND version = %s RETURNING *",
                (
                    status.value,
                    Jsonb(result) if result is not None else None,
                    error_code,
                    job_id,
                    expected_version,
                ),
            ).fetchone()
            if row is None:
                raise InvalidTransitionError("job missing or stale job version")
            return self._job(row)

    def append_audit(self, event: AuditEvent) -> None:
        with self.transaction(event.household_id):
            row = self.connection.execute(
                "SELECT sequence, event_hash FROM audit_events WHERE household_id = %s "
                "ORDER BY sequence DESC LIMIT 1",
                (event.household_id,),
            ).fetchone()
            if event.sequence != (row["sequence"] + 1 if row else 1):
                raise ValueError("audit sequence is not contiguous")
            if event.previous_hash != (row["event_hash"] if row else "0" * 64):
                raise ValueError("audit hash chain is invalid")
            self.connection.execute(
                "INSERT INTO audit_events (id, household_id, sequence, occurred_at, event_type, "
                "actor_id, correlation_id, resource_type, resource_id, payload, previous_hash, "
                "event_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.id,
                    event.household_id,
                    event.sequence,
                    event.occurred_at,
                    event.event_type,
                    event.actor_id,
                    event.correlation_id,
                    event.resource_type,
                    event.resource_id,
                    Jsonb(event.payload),
                    event.previous_hash,
                    event.event_hash,
                ),
            )

    def audit_events(self, household_id: UUID | None = None) -> tuple[AuditEvent, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM audit_events WHERE (%s::uuid IS NULL OR household_id = %s) "
                "ORDER BY household_id, sequence",
                (household_id, household_id),
            ).fetchall()
            return tuple(AuditEvent.model_validate(row) for row in rows)

    def add_outbox(self, event: OutboxEvent) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO outbox_events (id, aggregate_type, aggregate_id, event_type, "
                "schema_version, payload, correlation_id, causation_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event.event_type,
                    event.schema_version,
                    Jsonb(event.payload),
                    event.correlation_id,
                    event.causation_id,
                    event.created_at,
                ),
            )

    def outbox_events(self) -> tuple[OutboxEvent, ...]:
        with self.transaction():
            rows = self.connection.execute("SELECT * FROM outbox_events ORDER BY created_at, id")
            return tuple(OutboxEvent.model_validate(row) for row in rows)

    def publish_pending(self, deliver: Callable[[OutboxEvent], None], limit: int = 100) -> int:
        """At-least-once delivery: consumers must deduplicate by event ID.

        A crash after delivery but before commit can redeliver the event. Delivery
        callbacks must be bounded and must not call back into the Jarvis store.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        published = 0
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM outbox_events WHERE published_at IS NULL "
                "ORDER BY created_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            ).fetchall()
            for row in rows:
                event = OutboxEvent.model_validate(row).model_copy(
                    update={"attempts": row["attempts"] + 1}
                )
                self.connection.execute(
                    "UPDATE outbox_events SET attempts = attempts + 1 WHERE id = %s", (event.id,)
                )
                try:
                    deliver(event)
                except Exception:
                    continue
                self.connection.execute(
                    "UPDATE outbox_events SET published_at = now() WHERE id = %s", (event.id,)
                )
                published += 1
        return published

    def memberships(self, actor_id: UUID) -> tuple[Membership, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT m.user_id AS actor_id, m.household_id, m.role, u.display_name, "
                "h.name AS household_name FROM memberships m JOIN users u ON u.id = m.user_id "
                "JOIN households h ON h.id = m.household_id WHERE m.user_id = %s "
                "ORDER BY m.household_id",
                (actor_id,),
            )
            return tuple(Membership.model_validate(row) for row in rows)

    def put_membership(self, membership: Membership) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO users (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (membership.actor_id, membership.display_name),
            )
            self.connection.execute(
                "INSERT INTO households (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (membership.household_id, membership.household_name),
            )
            self.connection.execute(
                "INSERT INTO memberships (user_id, household_id, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (household_id, user_id) DO UPDATE SET role = EXCLUDED.role",
                (membership.actor_id, membership.household_id, membership.role),
            )

    def delete_membership(self, actor_id: UUID, household_id: UUID) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM memberships WHERE user_id = %s AND household_id = %s",
                (actor_id, household_id),
            )

    def save_session(self, session: Session) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
            self.connection.execute(
                "INSERT INTO auth_sessions (token_hash, actor_id, household_id, method, "
                "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    session.token_hash,
                    session.actor_id,
                    session.household_id,
                    session.method,
                    session.created_at,
                    session.expires_at,
                ),
            )

    def get_session(self, token_hash: str) -> Session | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = %s", (token_hash,)
            ).fetchone()
            return Session.model_validate(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = %s", (token_hash,)
            )

    def revoke_sessions(self, actor_id: UUID) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM auth_sessions WHERE actor_id = %s", (actor_id,))

    def save_enrollment(self, enrollment: Enrollment) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM auth_enrollments WHERE expires_at <= now()")
            self.connection.execute(
                "INSERT INTO auth_enrollments (token_hash, actor_id, household_id, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    enrollment.token_hash,
                    enrollment.actor_id,
                    enrollment.household_id,
                    enrollment.expires_at,
                ),
            )

    def get_enrollment(self, token_hash: str) -> Enrollment | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_enrollments WHERE token_hash = %s", (token_hash,)
            ).fetchone()
            return Enrollment.model_validate(row) if row else None

    def delete_enrollment(self, token_hash: str) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM auth_enrollments WHERE token_hash = %s", (token_hash,)
            )

    def save_challenge(self, challenge: Challenge) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM auth_challenges WHERE expires_at <= now()")
            self.connection.execute(
                "INSERT INTO auth_challenges (token_hash, binding_hash, challenge, kind, actor_id, "
                "household_id, enrollment_hash, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    challenge.token_hash,
                    challenge.binding_hash,
                    challenge.challenge,
                    challenge.kind,
                    challenge.actor_id,
                    challenge.household_id,
                    challenge.enrollment_hash,
                    challenge.expires_at,
                ),
            )

    def take_challenge(self, token_hash: str, binding_hash: str) -> Challenge | None:
        with self.transaction():
            row = self.connection.execute(
                "DELETE FROM auth_challenges WHERE token_hash = %s "
                "AND binding_hash = %s RETURNING *",
                (token_hash, binding_hash),
            ).fetchone()
            return Challenge.model_validate(row) if row else None

    def passkeys(self, actor_id: UUID) -> tuple[Passkey, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM auth_passkeys WHERE actor_id = %s", (actor_id,)
            )
            return tuple(Passkey.model_validate(row) for row in rows)

    def get_passkey(self, credential_id: str) -> Passkey | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_passkeys WHERE credential_id = %s", (credential_id,)
            ).fetchone()
            return Passkey.model_validate(row) if row else None

    def save_passkey(self, credential: Passkey) -> None:
        with self.transaction():
            row = self.connection.execute(
                "INSERT INTO auth_passkeys (credential_id, actor_id, public_key, sign_count, "
                "device_type, backed_up) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING credential_id",
                (
                    credential.credential_id,
                    credential.actor_id,
                    credential.public_key,
                    credential.sign_count,
                    credential.device_type,
                    credential.backed_up,
                ),
            ).fetchone()
            if row is None:
                raise AuthenticationError("passkey is already registered")

    def update_passkey(self, credential: Passkey) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE auth_passkeys SET sign_count = %s, backed_up = %s WHERE credential_id = %s",
                (credential.sign_count, credential.backed_up, credential.credential_id),
            )

    def delete_passkey(self, credential_id: str) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM auth_passkeys WHERE credential_id = %s", (credential_id,)
            )
