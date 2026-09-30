from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from threading import Lock
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from simon.adapters.memory import InMemoryStore
from simon.domain.accounts import ManagedAccount
from simon.domain.connected_tools import ActionProposal, GoogleConnection, GoogleOAuthState
from simon.domain.context import ExplicitMemory, RecallDocument
from simon.domain.conversations import Message, ModelAttempt, Run, RunEvent, Thread
from simon.domain.errors import (
    AuthenticationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.home import HomeCommand, HomeDevice, HomeSync, PowerSample
from simon.domain.identity import (
    Challenge,
    Enrollment,
    Membership,
    Passkey,
    PasswordCredential,
    Session,
)
from simon.domain.interaction import ResponsePreferences, RunFeedback
from simon.domain.models import AuditEvent, Job, JobStatus, OutboxEvent
from simon.domain.project_files import ProjectDrive, ProjectFileOperation
from simon.domain.tasks import ProjectArtifact
from simon.domain.voice import VoiceSession
from simon.domain.workflows import (
    WorkerHeartbeat,
    WorkflowDefinition,
    WorkflowEvent,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTrigger,
)


class PostgresStore(InMemoryStore):
    """Durable state with a process-local registry of executable capability handlers.

    Synchronous request threads get independent connections. Nested service/store calls
    share one transaction, using savepoints. Household locks serialize audit sequencing.
    External side effects are not covered by the database transaction.
    """

    def __init__(self, database_url: str, *, pool_size: int = 0) -> None:
        super().__init__()
        self._database_url = database_url
        self._pool_size = pool_size
        self._pool_lock = Lock()
        self._pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] | None = None
        self._connection: ContextVar[psycopg.Connection[dict[str, Any]] | None] = ContextVar(
            "simon_connection", default=None
        )

    def _connection_context(self) -> AbstractContextManager[psycopg.Connection[dict[str, Any]]]:
        if self._pool_size <= 0:
            return psycopg.connect(
                self._database_url, row_factory=dict_row, autocommit=True, connect_timeout=5
            )
        with self._pool_lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    self._database_url,
                    kwargs={"row_factory": dict_row, "autocommit": True, "connect_timeout": 5},
                    min_size=1,
                    max_size=self._pool_size,
                    timeout=10,
                    num_workers=1,
                    check=ConnectionPool.check_connection,
                    open=True,
                )
            pool = self._pool
        return pool.connection()

    def close(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    @contextmanager
    def transaction(self, household_id: UUID | None = None) -> Iterator[None]:
        current = self._connection.get()
        if current is not None:
            with current.transaction():
                if household_id is not None:
                    self._advisory_lock(f"household:{household_id}")
                yield
            return
        with self._connection_context() as connection:
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

    def workflow(self, identifier: UUID, version: int | None = None) -> WorkflowDefinition | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM workflow_versions WHERE id=%s AND deleted_at IS NULL "
                "AND (%s::int IS NULL "
                "OR version=%s) ORDER BY version DESC LIMIT 1",
                (identifier, version, version),
            ).fetchone()
            return WorkflowDefinition.model_validate(row["snapshot"]) if row else None

    def workflows(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowDefinition, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM (SELECT DISTINCT ON (id) id, snapshot FROM workflow_versions "
                "WHERE household_id=%s AND actor_id=%s AND deleted_at IS NULL "
                "ORDER BY id, version DESC) latest "
                "ORDER BY (snapshot->>'created_at')::timestamptz DESC, id DESC LIMIT %s OFFSET %s",
                (household_id, actor_id, limit, offset),
            ).fetchall()
            return tuple(WorkflowDefinition.model_validate(r["snapshot"]) for r in rows)

    def insert_workflow(self, definition: WorkflowDefinition) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO workflow_versions (id,version,household_id,actor_id,snapshot) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    definition.id,
                    definition.version,
                    definition.household_id,
                    definition.actor_id,
                    Jsonb(definition.model_dump(mode="json")),
                ),
            )

    def workflow_run(self, identifier: UUID) -> WorkflowRun | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM workflow_runs WHERE id=%s", (identifier,)
            ).fetchone()
            return WorkflowRun.model_validate(row["snapshot"]) if row else None

    def workflow_runs(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowRun, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_runs WHERE household_id=%s AND actor_id=%s "
                "ORDER BY (snapshot->>'created_at')::timestamptz DESC, id DESC LIMIT %s OFFSET %s",
                (household_id, actor_id, limit, offset),
            ).fetchall()
            return tuple(WorkflowRun.model_validate(r["snapshot"]) for r in rows)

    def save_workflow_run(self, run: WorkflowRun, expected_version: int) -> None:
        with self.transaction():
            if expected_version == 0:
                cursor = self.connection.execute(
                    "INSERT INTO workflow_runs (id,household_id,actor_id,definition_id,"
                    "definition_version,version,next_wake_at,snapshot) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        run.id,
                        run.household_id,
                        run.actor_id,
                        run.definition_id,
                        run.definition_version,
                        run.version,
                        run.next_wake_at,
                        Jsonb(run.model_dump(mode="json")),
                    ),
                )
            else:
                cursor = self.connection.execute(
                    "UPDATE workflow_runs SET version=%s,next_wake_at=%s,snapshot=%s WHERE "
                    "id=%s AND version=%s",
                    (
                        run.version,
                        run.next_wake_at,
                        Jsonb(run.model_dump(mode="json")),
                        run.id,
                        expected_version,
                    ),
                )
            if cursor.rowcount != 1:
                raise InvalidTransitionError("workflow run changed; reload before retrying")

    def due_workflows(self, now: datetime, limit: int) -> tuple[WorkflowRun, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_runs WHERE next_wake_at <= %s ORDER BY "
                "next_wake_at,id LIMIT %s",
                (now, limit),
            ).fetchall()
            return tuple(WorkflowRun.model_validate(r["snapshot"]) for r in rows)

    def append_workflow_event(self, event: WorkflowEvent) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO workflow_events (run_id,sequence,snapshot) VALUES (%s,%s,%s)",
                (event.run_id, event.sequence, Jsonb(event.model_dump(mode="json"))),
            )

    def workflow_events(self, run_id: UUID, after: int) -> tuple[WorkflowEvent, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_events WHERE run_id=%s AND sequence>%s "
                "ORDER BY sequence LIMIT 100",
                (run_id, after),
            ).fetchall()
            return tuple(WorkflowEvent.model_validate(r["snapshot"]) for r in rows)

    def worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO workflow_workers (id,seen_at) VALUES (%s,%s) ON CONFLICT (id) "
                "DO UPDATE SET seen_at=EXCLUDED.seen_at",
                (heartbeat.id, heartbeat.seen_at),
            )

    def workflow_workers(self) -> tuple[WorkerHeartbeat, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id,seen_at FROM workflow_workers ORDER BY seen_at DESC LIMIT 20"
            ).fetchall()
            return tuple(WorkerHeartbeat.model_validate(r) for r in rows)

    def workflow_schedule(self, identifier: UUID) -> WorkflowSchedule | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM workflow_schedules WHERE id=%s", (identifier,)
            ).fetchone()
            return WorkflowSchedule.model_validate(row["snapshot"]) if row else None

    def workflow_schedules(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowSchedule, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_schedules WHERE household_id=%s "
                "AND actor_id=%s ORDER BY (snapshot->>'created_at')::timestamptz DESC, "
                "id DESC LIMIT %s OFFSET %s",
                (household_id, actor_id, limit, offset),
            ).fetchall()
            return tuple(WorkflowSchedule.model_validate(row["snapshot"]) for row in rows)

    def due_workflow_schedules(self, now: datetime, limit: int) -> tuple[WorkflowSchedule, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_schedules WHERE next_run_at <= %s "
                "ORDER BY next_run_at,id LIMIT %s",
                (now, limit),
            ).fetchall()
            return tuple(WorkflowSchedule.model_validate(row["snapshot"]) for row in rows)

    def save_workflow_schedule(self, schedule: WorkflowSchedule, expected_version: int) -> None:
        with self.transaction():
            if expected_version == 0:
                cursor = self.connection.execute(
                    "INSERT INTO workflow_schedules (id,household_id,actor_id,definition_id,"
                    "definition_version,version,next_run_at,snapshot) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        schedule.id,
                        schedule.household_id,
                        schedule.actor_id,
                        schedule.definition_id,
                        schedule.definition_version,
                        schedule.version,
                        schedule.next_run_at,
                        Jsonb(schedule.model_dump(mode="json")),
                    ),
                )
            else:
                cursor = self.connection.execute(
                    "UPDATE workflow_schedules SET version=%s,next_run_at=%s,snapshot=%s "
                    "WHERE id=%s AND version=%s",
                    (
                        schedule.version,
                        schedule.next_run_at,
                        Jsonb(schedule.model_dump(mode="json")),
                        schedule.id,
                        expected_version,
                    ),
                )
            if cursor.rowcount != 1:
                raise InvalidTransitionError("workflow schedule changed; reload before retrying")

    def delete_workflow_schedule(
        self, identifier: UUID, household_id: UUID, actor_id: UUID
    ) -> bool:
        with self.transaction():
            cursor = self.connection.execute(
                "DELETE FROM workflow_schedules WHERE id=%s AND household_id=%s AND actor_id=%s",
                (identifier, household_id, actor_id),
            )
            return cursor.rowcount == 1

    def delete_workflow_schedules(
        self, definition_id: UUID, household_id: UUID, actor_id: UUID
    ) -> int:
        with self.transaction():
            cursor = self.connection.execute(
                "DELETE FROM workflow_schedules WHERE definition_id=%s AND household_id=%s "
                "AND actor_id=%s",
                (definition_id, household_id, actor_id),
            )
            return cursor.rowcount

    def workflow_trigger(self, identifier: UUID) -> WorkflowTrigger | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM workflow_triggers WHERE id=%s", (identifier,)
            ).fetchone()
            return WorkflowTrigger.model_validate(row["snapshot"]) if row else None

    def workflow_trigger_for_definition(self, definition_id: UUID) -> WorkflowTrigger | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM workflow_triggers WHERE definition_id=%s", (definition_id,)
            ).fetchone()
            return WorkflowTrigger.model_validate(row["snapshot"]) if row else None

    def workflow_triggers_for_definition(self, definition_id: UUID) -> tuple[WorkflowTrigger, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_triggers WHERE definition_id=%s ORDER BY id",
                (definition_id,),
            ).fetchall()
            return tuple(WorkflowTrigger.model_validate(row["snapshot"]) for row in rows)

    def workflow_triggers(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowTrigger, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_triggers WHERE household_id=%s AND actor_id=%s "
                "ORDER BY (snapshot->>'created_at')::timestamptz DESC,id DESC LIMIT %s OFFSET %s",
                (household_id, actor_id, limit, offset),
            ).fetchall()
            return tuple(WorkflowTrigger.model_validate(row["snapshot"]) for row in rows)

    def due_workflow_triggers(self, now: datetime, limit: int) -> tuple[WorkflowTrigger, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM workflow_triggers WHERE next_check_at <= %s "
                "ORDER BY next_check_at,id LIMIT %s",
                (now, limit),
            ).fetchall()
            return tuple(WorkflowTrigger.model_validate(row["snapshot"]) for row in rows)

    def save_workflow_trigger(self, trigger: WorkflowTrigger, expected_version: int) -> None:
        with self.transaction():
            if expected_version == 0:
                cursor = self.connection.execute(
                    "INSERT INTO workflow_triggers (id,household_id,actor_id,definition_id,"
                    "definition_version,version,next_check_at,snapshot) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        trigger.id,
                        trigger.household_id,
                        trigger.actor_id,
                        trigger.definition_id,
                        trigger.definition_version,
                        trigger.version,
                        trigger.next_check_at,
                        Jsonb(trigger.model_dump(mode="json")),
                    ),
                )
            else:
                cursor = self.connection.execute(
                    "UPDATE workflow_triggers SET definition_version=%s,version=%s,"
                    "next_check_at=%s,snapshot=%s WHERE id=%s AND version=%s",
                    (
                        trigger.definition_version,
                        trigger.version,
                        trigger.next_check_at,
                        Jsonb(trigger.model_dump(mode="json")),
                        trigger.id,
                        expected_version,
                    ),
                )
            if cursor.rowcount != 1:
                raise InvalidTransitionError("workflow trigger changed; reload before retrying")

    def delete_workflow_trigger(self, identifier: UUID, household_id: UUID, actor_id: UUID) -> bool:
        with self.transaction():
            cursor = self.connection.execute(
                "DELETE FROM workflow_triggers WHERE id=%s AND household_id=%s AND actor_id=%s",
                (identifier, household_id, actor_id),
            )
            return cursor.rowcount == 1

    def delete_workflow_trigger_for_definition(
        self, definition_id: UUID, household_id: UUID, actor_id: UUID
    ) -> int:
        with self.transaction():
            cursor = self.connection.execute(
                "DELETE FROM workflow_triggers WHERE definition_id=%s AND household_id=%s "
                "AND actor_id=%s",
                (definition_id, household_id, actor_id),
            )
            return cursor.rowcount

    def save_project_artifact(self, artifact: ProjectArtifact, content: bytes) -> None:
        with self.transaction():
            cursor = self.connection.execute(
                "INSERT INTO project_artifacts (id,household_id,actor_id,project_id,task_id,"
                "name,media_type,byte_count,sha256,content,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    artifact.id,
                    artifact.household_id,
                    artifact.actor_id,
                    artifact.project_id,
                    artifact.task_id,
                    artifact.name,
                    artifact.media_type,
                    artifact.byte_count,
                    artifact.sha256,
                    content,
                    artifact.created_at,
                ),
            )
            if cursor.rowcount != 1:
                row = self.connection.execute(
                    "SELECT sha256 FROM project_artifacts WHERE id=%s", (artifact.id,)
                ).fetchone()
                if not row or row["sha256"] != artifact.sha256:
                    raise InvalidTransitionError("project artifact already exists")

    def project_artifact(self, identifier: UUID) -> tuple[ProjectArtifact, bytes] | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT id,household_id,actor_id,project_id,task_id,name,media_type,"
                "byte_count,sha256,created_at,content FROM project_artifacts WHERE id=%s",
                (identifier,),
            ).fetchone()
            if not row:
                return None
            content = bytes(row.pop("content"))
            return ProjectArtifact.model_validate(row), content

    def project_artifacts(
        self,
        household_id: UUID,
        actor_id: UUID,
        project_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[ProjectArtifact, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id,household_id,actor_id,project_id,task_id,name,media_type,"
                "byte_count,sha256,created_at FROM project_artifacts WHERE household_id=%s "
                "AND actor_id=%s AND (%s::uuid IS NULL OR project_id=%s) "
                "ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (household_id, actor_id, project_id, project_id, limit, offset),
            ).fetchall()
            return tuple(ProjectArtifact.model_validate(row) for row in rows)

    def home_command(
        self, household_id: UUID, actor_id: UUID, command_id: UUID
    ) -> HomeCommand | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM home_commands WHERE household_id = %s "
                "AND actor_id = %s AND id = %s",
                (household_id, actor_id, command_id),
            ).fetchone()
            return HomeCommand.model_validate(row["snapshot"]) if row else None

    def power_samples(
        self, household_id: UUID, device_id: str, start: datetime, end: datetime, limit: int = 3000
    ) -> tuple[PowerSample, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM power_samples WHERE household_id = %s AND device_id = %s "
                "AND captured_at >= %s AND captured_at < %s ORDER BY captured_at DESC LIMIT %s",
                (household_id, device_id, start, end, limit),
            ).fetchall()
            return tuple(PowerSample.model_validate(row["snapshot"]) for row in rows)

    def save_power_sample(self, sample: PowerSample) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO power_samples (household_id, device_id, captured_at, snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (household_id, device_id, captured_at) "
                "DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    sample.household_id,
                    sample.device_id,
                    sample.captured_at,
                    Jsonb(sample.model_dump(mode="json")),
                ),
            )

    def prune_power_samples(self, household_id: UUID, before: datetime) -> None:
        with self.transaction():
            self.connection.execute(
                "DELETE FROM power_samples WHERE household_id = %s AND captured_at < %s",
                (household_id, before),
            )

    def home_commands(
        self,
        household_id: UUID,
        actor_id: UUID,
        run_id: UUID | None = None,
        *,
        thread_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[HomeCommand, ...]:
        with self.transaction():
            query = "SELECT snapshot FROM home_commands WHERE household_id = %s AND actor_id = %s"
            params: list[UUID | int] = [household_id, actor_id]
            if run_id is not None:
                query += " AND run_id = %s"
                params.append(run_id)
            if thread_id is not None:
                query += " AND thread_id = %s"
                params.append(thread_id)
            params.append(limit)
            rows = self.connection.execute(
                query + " ORDER BY created_at DESC, id DESC LIMIT %s", params
            ).fetchall()
            return tuple(HomeCommand.model_validate(row["snapshot"]) for row in rows)

    def save_home_command(self, command: HomeCommand) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO home_commands (id, household_id, actor_id, thread_id, run_id, "
                "created_at, snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    command.id,
                    command.household_id,
                    command.actor_id,
                    command.thread_id,
                    command.run_id,
                    command.created_at,
                    Jsonb(command.model_dump(mode="json")),
                ),
            )

    def managed_accounts(self) -> tuple[ManagedAccount, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM managed_accounts ORDER BY actor_id"
            ).fetchall()
            return tuple(ManagedAccount.model_validate(row["snapshot"]) for row in rows)

    def managed_account(self, actor_id: UUID) -> ManagedAccount | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM managed_accounts WHERE actor_id = %s", (actor_id,)
            ).fetchone()
            return ManagedAccount.model_validate(row["snapshot"]) if row else None

    def save_managed_account(self, account: ManagedAccount) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO managed_accounts (actor_id, household_id, invited_by, snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (actor_id) "
                "DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    account.actor_id,
                    account.household_id,
                    account.invited_by,
                    Jsonb(account.model_dump(mode="json")),
                ),
            )

    def voice_sessions(self, household_id: UUID, actor_id: UUID) -> tuple[VoiceSession, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM voice_sessions WHERE household_id = %s AND actor_id = %s "
                "ORDER BY created_at DESC LIMIT 50",
                (household_id, actor_id),
            ).fetchall()
            return tuple(VoiceSession.model_validate(row["snapshot"]) for row in rows)

    def voice_session(self, household_id: UUID, session_id: UUID) -> VoiceSession | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM voice_sessions WHERE household_id = %s AND id = %s",
                (household_id, session_id),
            ).fetchone()
            return VoiceSession.model_validate(row["snapshot"]) if row else None

    def save_voice_session(self, session: VoiceSession) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO voice_sessions (id, household_id, actor_id, thread_id, created_at, "
                "snapshot) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE "
                "SET snapshot = EXCLUDED.snapshot",
                (
                    session.id,
                    session.household_id,
                    session.actor_id,
                    session.thread_id,
                    session.created_at,
                    Jsonb(session.model_dump(mode="json")),
                ),
            )

    def home_devices(self, household_id: UUID) -> tuple[HomeDevice, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM home_devices WHERE household_id = %s ORDER BY id",
                (household_id,),
            ).fetchall()
            return tuple(HomeDevice.model_validate(row["snapshot"]) for row in rows)

    def save_home_device(self, device: HomeDevice) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO home_devices (household_id, id, snapshot) VALUES (%s, %s, %s) "
                "ON CONFLICT (household_id, id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (device.household_id, device.id, Jsonb(device.model_dump(mode="json"))),
            )

    def home_sync(self, household_id: UUID, provider: str) -> HomeSync | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM home_syncs WHERE household_id = %s AND provider = %s",
                (household_id, provider),
            ).fetchone()
            return HomeSync.model_validate(row["snapshot"]) if row else None

    def save_home_sync(self, sync: HomeSync) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO home_syncs (household_id, provider, snapshot) VALUES (%s, %s, %s) "
                "ON CONFLICT (household_id, provider) DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (sync.household_id, sync.provider, Jsonb(sync.model_dump(mode="json"))),
            )

    def response_preferences(
        self, household_id: UUID, actor_id: UUID
    ) -> ResponsePreferences | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM response_preferences "
                "WHERE household_id = %s AND actor_id = %s",
                (household_id, actor_id),
            ).fetchone()
            return ResponsePreferences.model_validate(row["snapshot"]) if row else None

    def save_response_preferences(
        self, household_id: UUID, actor_id: UUID, preferences: ResponsePreferences
    ) -> None:
        with self.transaction(household_id):
            self.connection.execute(
                "INSERT INTO response_preferences (household_id, actor_id, snapshot) "
                "VALUES (%s,%s,%s) ON CONFLICT (household_id, actor_id) DO UPDATE "
                "SET snapshot = EXCLUDED.snapshot",
                (household_id, actor_id, Jsonb(preferences.model_dump(mode="json"))),
            )

    def feedback(self, household_id: UUID, actor_id: UUID, run_id: UUID) -> RunFeedback | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM run_feedback "
                "WHERE household_id = %s AND actor_id = %s AND run_id = %s",
                (household_id, actor_id, run_id),
            ).fetchone()
            return RunFeedback.model_validate(row["snapshot"]) if row else None

    def save_feedback(self, household_id: UUID, actor_id: UUID, feedback: RunFeedback) -> None:
        with self.transaction(household_id):
            self.connection.execute(
                "INSERT INTO run_feedback (household_id, actor_id, run_id, snapshot) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (household_id, actor_id, run_id) DO UPDATE "
                "SET snapshot = EXCLUDED.snapshot",
                (household_id, actor_id, feedback.run_id, Jsonb(feedback.model_dump(mode="json"))),
            )

    def answer_runs(self, thread_id: UUID, offset: int, limit: int) -> tuple[Run, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM runs WHERE thread_id = %s AND snapshot IS NOT NULL "
                "ORDER BY created_at, id OFFSET %s LIMIT %s",
                (thread_id, offset, limit),
            ).fetchall()
            return tuple(Run.model_validate(row["snapshot"]) for row in rows)

    def latest_run(self, thread_id: UUID) -> Run | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM runs WHERE thread_id = %s AND snapshot IS NOT NULL "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            return Run.model_validate(row["snapshot"]) if row else None

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
                "VALUES (%s,%s,%s,%s,'explicit',%s,%s,1,'personal','accepted',%s,%s)",
                (
                    memory.id,
                    memory.household_id,
                    memory.subject,
                    memory.scope,
                    memory.content,
                    Jsonb({"type": memory.source, "actor_id": str(memory.created_by)}),
                    memory.created_at,
                    Jsonb(memory.model_dump(mode="json")),
                ),
            )

    def explicit_memories(
        self,
        household_id: UUID,
        offset: int,
        limit: int,
        actor_id: UUID | None = None,
        personal: bool = True,
    ) -> tuple[ExplicitMemory, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT explicit_snapshot FROM memories WHERE household_id = %s "
                "AND explicit_snapshot IS NOT NULL AND review_status = 'accepted' "
                "AND (scope = 'household' OR (%s AND (%s::uuid IS NULL OR "
                "explicit_snapshot->>'created_by' = %s))) "
                "ORDER BY created_at, id LIMIT %s OFFSET %s",
                (household_id, personal, actor_id, str(actor_id), limit, offset),
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
                "INSERT INTO threads (id, household_id, created_by, title, created_at, visibility) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    thread.id,
                    thread.household_id,
                    thread.created_by,
                    thread.title,
                    thread.created_at,
                    thread.visibility,
                ),
            )

    def threads(
        self, household_id: UUID, offset: int, limit: int, actor_id: UUID | None = None
    ) -> tuple[Thread, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id, household_id, created_by, title, created_at, visibility FROM threads "
                "WHERE household_id = %s AND (%s::uuid IS NULL OR visibility = 'household' "
                "OR created_by = %s) ORDER BY created_at, id LIMIT %s OFFSET %s",
                (household_id, actor_id, actor_id, limit, offset),
            ).fetchall()
            return tuple(Thread.model_validate(row) for row in rows)

    def thread(self, household_id: UUID, thread_id: UUID) -> Thread | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT id, household_id, created_by, title, created_at, visibility FROM threads "
                "WHERE household_id = %s AND id = %s",
                (household_id, thread_id),
            ).fetchone()
            return Thread.model_validate(row) if row else None

    def recall_documents(
        self,
        household_id: UUID,
        actor_id: UUID,
        terms: tuple[str, ...],
        offset: int,
        limit: int,
        exclude_thread: UUID | None = None,
    ) -> tuple[RecallDocument, ...]:
        with self.transaction():
            rows = self.connection.execute(
                """WITH owned AS (
                    SELECT * FROM threads WHERE household_id = %(household)s
                    AND created_by = %(actor)s
                    AND (%(exclude)s::uuid IS NULL OR id != %(exclude)s)
                ), docs AS (
                    SELECT m.id, m.thread_id, t.title, 'text' AS source, m.role,
                           m.content->>'text' AS text, m.created_at
                    FROM messages m JOIN owned t ON t.id = m.thread_id
                    WHERE NOT EXISTS (SELECT 1 FROM voice_sessions v WHERE v.thread_id = t.id)
                    UNION ALL
                    SELECT v.id, v.thread_id, t.title, 'voice', 'transcript', transcript.text,
                           v.created_at
                    FROM voice_sessions v JOIN owned t ON t.id = v.thread_id
                    CROSS JOIN LATERAL (
                        SELECT string_agg(
                            CASE WHEN prev IS DISTINCT FROM f->>'speaker'
                                THEN E'\\n' || (f->>'speaker') || ': ' ELSE '' END
                            || (f->>'text'), '' ORDER BY ord) AS text
                        FROM (
                            SELECT f, ord, lag(f->>'speaker') OVER (ORDER BY ord) AS prev
                            FROM jsonb_array_elements(v.snapshot->'fragments')
                                WITH ORDINALITY AS fragments(f, ord)
                        ) parts
                    ) transcript
                    WHERE v.household_id = %(household)s AND v.actor_id = %(actor)s
                    AND transcript.text IS NOT NULL
                ), ranked AS (
                    SELECT docs.*, (SELECT count(*) FROM unnest(%(terms)s::text[]) term
                        WHERE strpos(lower(title || ' ' || text), term) > 0) AS score
                    FROM docs
                ) SELECT id, thread_id, title, source, role, text, created_at FROM ranked
                WHERE cardinality(%(terms)s::text[]) = 0 OR score > 0
                ORDER BY score DESC, created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s
                """,
                {
                    "household": household_id,
                    "actor": actor_id,
                    "exclude": exclude_thread,
                    "terms": list(terms),
                    "limit": limit,
                    "offset": offset,
                },
            ).fetchall()
            return tuple(RecallDocument.model_validate(row) for row in rows)

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

    def jobs(
        self, household_id: UUID, actor_id: UUID, kind: str, offset: int, limit: int
    ) -> tuple[Job, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE household_id=%s AND created_by=%s AND kind=%s "
                "ORDER BY COALESCE((input->>'priority')::int,3) DESC, "
                "COALESCE((input->>'rank')::bigint,0), created_at, id OFFSET %s LIMIT %s",
                (household_id, actor_id, kind, offset, limit),
            ).fetchall()
            return tuple(self._job(row) for row in rows)

    def jobs_all(self, kind: str, limit: int, status: str = "queued") -> tuple[Job, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE kind=%s AND status=%s ORDER BY "
                "COALESCE((input->>'priority')::int,3) DESC, "
                "COALESCE((input->>'rank')::bigint,0), created_at, id LIMIT %s",
                (kind, status, limit),
            ).fetchall()
            return tuple(self._job(row) for row in rows)

    def save_job(self, job: Job, expected_version: int) -> Job:
        with self.transaction():
            row = self.connection.execute(
                "UPDATE jobs SET input=%s,input_digest=%s,status=%s,result=%s,error_code=%s,"
                "version=version+1,updated_at=%s WHERE id=%s AND version=%s RETURNING *",
                (
                    Jsonb(job.input),
                    job.input_digest,
                    job.status.value,
                    Jsonb(job.result) if job.result is not None else None,
                    job.error_code,
                    job.updated_at,
                    job.id,
                    expected_version,
                ),
            ).fetchone()
            if row is None:
                raise InvalidTransitionError("job missing or stale job version")
            return self._job(row)

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
        callbacks must be bounded and must not call back into the Simon store.
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

    def delete_workflow(self, identifier: UUID, household_id: UUID, actor_id: UUID) -> bool:
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE workflow_versions SET deleted_at=now() WHERE id=%s "
                "AND household_id=%s AND actor_id=%s AND deleted_at IS NULL",
                (identifier, household_id, actor_id),
            )
            return cursor.rowcount > 0

    def password_for_actor(self, actor_id: UUID) -> PasswordCredential | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_passwords WHERE actor_id = %s", (actor_id,)
            ).fetchone()
            return PasswordCredential.model_validate(row) if row else None

    def password_for_username(self, username: str) -> PasswordCredential | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_passwords WHERE username = %s", (username,)
            ).fetchone()
            return PasswordCredential.model_validate(row) if row else None

    def save_password(self, credential: PasswordCredential) -> None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT actor_id FROM auth_passwords WHERE username = %s", (credential.username,)
            ).fetchone()
            if row and row["actor_id"] != credential.actor_id:
                raise AuthenticationError("username is already in use")
            self.connection.execute(
                "INSERT INTO auth_passwords "
                "(actor_id, username, password_hash, failed_attempts, locked_until) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (actor_id) DO UPDATE SET "
                "username = EXCLUDED.username, password_hash = EXCLUDED.password_hash, "
                "failed_attempts = EXCLUDED.failed_attempts, locked_until = EXCLUDED.locked_until",
                (
                    credential.actor_id,
                    credential.username,
                    credential.password_hash,
                    credential.failed_attempts,
                    credential.locked_until,
                ),
            )

    def google_connections(
        self, household_id: UUID, actor_id: UUID
    ) -> tuple[GoogleConnection, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM google_connections WHERE household_id=%s AND actor_id=%s "
                "ORDER BY (snapshot->>'is_default')::boolean DESC, email",
                (household_id, actor_id),
            ).fetchall()
            return tuple(GoogleConnection.model_validate(row["snapshot"]) for row in rows)

    def google_connection(self, household_id: UUID, actor_id: UUID) -> GoogleConnection | None:
        connections = self.google_connections(household_id, actor_id)
        return connections[0] if connections else None

    def save_google_connection(self, connection: GoogleConnection) -> None:
        with self.transaction(connection.household_id):
            connections = self.google_connections(connection.household_id, connection.actor_id)
            old = next(
                (c for c in connections if c.email.casefold() == connection.email.casefold()), None
            )
            connection = connection.model_copy(
                update={"is_default": old.is_default if old else not connections}
            )
            self.connection.execute(
                "INSERT INTO google_connections (household_id, actor_id, email, snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (household_id, actor_id, email) "
                "DO UPDATE SET snapshot=EXCLUDED.snapshot",
                (
                    connection.household_id,
                    connection.actor_id,
                    connection.email.casefold(),
                    Jsonb(connection.model_dump(mode="json")),
                ),
            )

    def set_default_google_connection(
        self, household_id: UUID, actor_id: UUID, connection_id: UUID
    ) -> None:
        with self.transaction(household_id):
            connections = self.google_connections(household_id, actor_id)
            if not any(c.id == connection_id for c in connections):
                raise NotFoundError("Google account not found.")
            self.connection.execute(
                "UPDATE google_connections SET snapshot=jsonb_set(snapshot, '{is_default}', "
                "to_jsonb((snapshot->>'id')::uuid=%s)) WHERE household_id=%s AND actor_id=%s",
                (connection_id, household_id, actor_id),
            )

    def delete_google_connection(
        self, household_id: UUID, actor_id: UUID, connection_id: UUID | None = None
    ) -> None:
        with self.transaction(household_id):
            self.connection.execute(
                "DELETE FROM google_connections WHERE household_id=%s AND actor_id=%s "
                "AND (%s::uuid IS NULL OR (snapshot->>'id')::uuid=%s)",
                (household_id, actor_id, connection_id, connection_id),
            )
            remaining = self.google_connections(household_id, actor_id)
            if remaining and not any(c.is_default for c in remaining):
                self.set_default_google_connection(household_id, actor_id, remaining[0].id)

    def save_google_state(self, state: GoogleOAuthState) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM google_oauth_states WHERE expires_at <= now()")
            self.connection.execute(
                "INSERT INTO google_oauth_states (state_hash, binding_hash, expires_at, snapshot) "
                "VALUES (%s, %s, %s, %s)",
                (
                    state.state_hash,
                    state.binding_hash,
                    state.expires_at,
                    Jsonb(state.model_dump(mode="json")),
                ),
            )

    def take_google_state(self, state_hash: str, binding_hash: str) -> GoogleOAuthState | None:
        with self.transaction():
            row = self.connection.execute(
                "DELETE FROM google_oauth_states WHERE state_hash = %s "
                "AND binding_hash = %s RETURNING snapshot",
                (state_hash, binding_hash),
            ).fetchone()
            return GoogleOAuthState.model_validate(row["snapshot"]) if row else None

    def google_accounts(self) -> tuple[tuple[UUID, UUID], ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT DISTINCT household_id, actor_id FROM google_connections ORDER BY actor_id"
            ).fetchall()
            return tuple((row["household_id"], row["actor_id"]) for row in rows)

    def project_drive(
        self,
        household_id: UUID,
        actor_id: UUID,
        project_id: UUID,
    ) -> ProjectDrive | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM project_drive "
                "WHERE household_id = %s AND actor_id = %s AND project_id = %s",
                (household_id, actor_id, project_id),
            ).fetchone()
            return ProjectDrive.model_validate(row["snapshot"]) if row else None

    def save_project_drive(self, binding: ProjectDrive) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO project_drive (household_id, actor_id, project_id, snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (household_id, actor_id, project_id) "
                "DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    binding.household_id,
                    binding.actor_id,
                    binding.project_id,
                    Jsonb(binding.model_dump(mode="json")),
                ),
            )

    def project_file_operation(self, identifier: UUID) -> ProjectFileOperation | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM project_file_operations WHERE id = %s",
                (identifier,),
            ).fetchone()
            return ProjectFileOperation.model_validate(row["snapshot"]) if row else None

    def save_project_file_operation(self, operation: ProjectFileOperation) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO project_file_operations "
                "(id, household_id, actor_id, project_id, snapshot) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) "
                "DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    operation.id,
                    operation.household_id,
                    operation.actor_id,
                    operation.project_id,
                    Jsonb(operation.model_dump(mode="json")),
                ),
            )

    def project_file_operations(
        self,
        household_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        limit: int,
    ) -> tuple[ProjectFileOperation, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM project_file_operations "
                "WHERE household_id = %s AND actor_id = %s AND project_id = %s "
                "ORDER BY snapshot->>'created_at' DESC, id DESC LIMIT %s",
                (household_id, actor_id, project_id, limit),
            ).fetchall()
            return tuple(ProjectFileOperation.model_validate(row["snapshot"]) for row in rows)

    def action(self, action_id: UUID) -> ActionProposal | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM action_proposals WHERE id = %s", (action_id,)
            ).fetchone()
            return ActionProposal.model_validate(row["snapshot"]) if row else None

    def recent_actions(
        self, thread_id: UUID, actor_id: UUID, limit: int
    ) -> tuple[ActionProposal, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT a.snapshot FROM action_proposals a "
                "LEFT JOIN runs r ON r.id = a.run_id "
                "LEFT JOIN model_attempts m ON m.id = a.attempt_id "
                "JOIN threads t ON t.id = COALESCE(r.thread_id, m.thread_id) "
                "WHERE t.id = %s AND a.actor_id = %s "
                "AND a.household_id = t.household_id "
                "AND (a.attempt_id IS NOT NULL OR r.snapshot->'action_ids' ? a.id::text) "
                "ORDER BY a.snapshot->>'created_at' DESC, a.id DESC LIMIT %s",
                (thread_id, actor_id, limit),
            ).fetchall()
            return tuple(ActionProposal.model_validate(row["snapshot"]) for row in rows)

    def save_action(self, action: ActionProposal) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO action_proposals "
                "(id, household_id, actor_id, run_id, attempt_id, snapshot) "
                "VALUES (%s, %s, %s, "
                "CASE WHEN EXISTS (SELECT 1 FROM runs WHERE id = %s) THEN %s END, "
                "CASE WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id = %s) THEN %s END, %s) "
                "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    action.id,
                    action.household_id,
                    action.actor_id,
                    action.run_id,
                    action.run_id,
                    action.run_id,
                    action.run_id,
                    Jsonb(action.model_dump(mode="json")),
                ),
            )
