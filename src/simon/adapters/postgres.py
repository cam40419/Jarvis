"""PostgreSQL persistence with scoped transactions and optimistic state transitions."""

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
from simon.domain.email_identity import EmailCode
from simon.domain.errors import (
    AuthenticationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.identity import (
    Challenge,
    Enrollment,
    Membership,
    Passkey,
    PasswordCredential,
    Session,
)
from simon.domain.integrations import IntegrationConnection
from simon.domain.interaction import ResponsePreferences, RunFeedback
from simon.domain.models import AuditEvent, Job, JobStatus, OutboxEvent
from simon.domain.native_agents import NativeAgent, NativeAgentCredential, NativeTeamPolicy
from simon.domain.native_intake import (
    INTAKE_RUN_MUTABLE_FIELDS,
    IntakeRun,
    IntakeSource,
    NativeIntake,
)
from simon.domain.native_projects import NativeProject, NativeProjectMember, NativeTask
from simon.domain.voice import VoiceSession


class PostgresStore(InMemoryStore):
    """Durable state with a process-local registry of executable capability handlers.

    Synchronous request threads get independent connections. Nested service/store calls
    share one transaction, using savepoints. Workspace locks serialize audit sequencing.
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
    def transaction(self, workspace_id: UUID | None = None) -> Iterator[None]:
        current = self._connection.get()
        if current is not None:
            with current.transaction():
                if workspace_id is not None:
                    self._advisory_lock(f"workspace:{workspace_id}")
                yield
            return
        with self._connection_context() as connection:
            token = self._connection.set(connection)
            try:
                with connection.transaction():
                    connection.execute("SET LOCAL lock_timeout = '10s'")
                    connection.execute("SET LOCAL statement_timeout = '30s'")
                    if workspace_id is not None:
                        self._advisory_lock(f"workspace:{workspace_id}")
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

    def native_project(self, workspace_id: UUID, project_id: UUID) -> NativeProject | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM native_projects WHERE workspace_id=%s AND id=%s",
                (workspace_id, project_id),
            ).fetchone()
            return NativeProject.model_validate(row) if row else None

    def native_projects(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        offset: int,
        limit: int,
        *,
        all_projects: bool = False,
    ) -> tuple[NativeProject, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT p.* FROM native_projects p WHERE p.workspace_id=%s AND (%s OR EXISTS ("
                "SELECT 1 FROM native_project_members m WHERE m.workspace_id=p.workspace_id "
                "AND m.project_id=p.id AND m.actor_id=%s)) "
                "ORDER BY p.created_at DESC,p.id DESC LIMIT %s OFFSET %s",
                (workspace_id, all_projects, actor_id, limit, offset),
            ).fetchall()
            return tuple(NativeProject.model_validate(row) for row in rows)

    def insert_native_project(self, project: NativeProject) -> None:
        if project.version != 1:
            raise InvalidTransitionError("Native projects must start at version one")
        try:
            with self.transaction():
                result = self.connection.execute(
                    "INSERT INTO native_projects (id,workspace_id,name,objective,status,"
                    "board_authority,created_by,version,created_at,updated_at) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE EXISTS ("
                    "SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s)",
                    (
                        project.id,
                        project.workspace_id,
                        project.name,
                        project.objective,
                        project.status,
                        project.board_authority,
                        project.created_by,
                        project.version,
                        project.created_at,
                        project.updated_at,
                        project.workspace_id,
                        project.created_by,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Native project creator is unavailable")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Native project already exists or creator is unavailable"
            ) from exc

    def update_native_project(self, project: NativeProject, expected_version: int) -> None:
        if project.version != expected_version + 1:
            raise InvalidTransitionError("Native project update must advance the version")
        with self.transaction():
            result = self.connection.execute(
                "UPDATE native_projects SET name=%s,objective=%s,status=%s,"
                "version=%s,updated_at=%s "
                "WHERE workspace_id=%s AND id=%s AND version=%s "
                "AND created_by=%s AND created_at=%s AND board_authority=%s",
                (
                    project.name,
                    project.objective,
                    project.status,
                    project.version,
                    project.updated_at,
                    project.workspace_id,
                    project.id,
                    expected_version,
                    project.created_by,
                    project.created_at,
                    project.board_authority,
                ),
            )
            if result.rowcount != 1:
                raise InvalidTransitionError("Native project missing or stale project version")

    def native_project_members(
        self, workspace_id: UUID, project_id: UUID
    ) -> tuple[NativeProjectMember, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM native_project_members WHERE workspace_id=%s AND project_id=%s "
                "ORDER BY created_at,actor_id",
                (workspace_id, project_id),
            ).fetchall()
            return tuple(NativeProjectMember.model_validate(row) for row in rows)

    def native_project_member(
        self, workspace_id: UUID, project_id: UUID, actor_id: UUID
    ) -> NativeProjectMember | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM native_project_members "
                "WHERE workspace_id=%s AND project_id=%s AND actor_id=%s",
                (workspace_id, project_id, actor_id),
            ).fetchone()
            return NativeProjectMember.model_validate(row) if row else None

    def put_native_project_member(self, member: NativeProjectMember) -> None:
        try:
            with self.transaction():
                self.connection.execute(
                    "INSERT INTO native_project_members "
                    "(workspace_id,project_id,actor_id,role,created_at) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (workspace_id,project_id,actor_id) "
                    "DO UPDATE SET role=EXCLUDED.role",
                    (
                        member.workspace_id,
                        member.project_id,
                        member.actor_id,
                        member.role,
                        member.created_at,
                    ),
                )
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Native project or workspace member is unavailable"
            ) from exc

    def delete_native_project_member(
        self, workspace_id: UUID, project_id: UUID, actor_id: UUID
    ) -> None:
        try:
            with self.transaction():
                self.connection.execute(
                    "DELETE FROM native_project_members "
                    "WHERE workspace_id=%s AND project_id=%s AND actor_id=%s",
                    (workspace_id, project_id, actor_id),
                )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise InvalidTransitionError(
                "Reassign this member's tasks before removing them"
            ) from exc

    def native_assigned_task_exists(
        self, workspace_id: UUID, project_id: UUID, actor_id: UUID
    ) -> bool:
        with self.transaction():
            row = self.connection.execute(
                "SELECT EXISTS (SELECT 1 FROM native_tasks "
                "WHERE workspace_id=%s AND project_id=%s AND assignee_actor_id=%s) AS present",
                (workspace_id, project_id, actor_id),
            ).fetchone()
            return bool(row and row["present"])

    @staticmethod
    def _native_task(row: dict[str, Any]) -> NativeTask:
        value = dict(row)
        value["assignment"] = {
            "kind": value.pop("assignment_kind"),
            "actor_id": value.pop("assignee_actor_id"),
            "agent_id": value.pop("assignee_agent_id"),
        }
        return NativeTask.model_validate(value)

    def native_task(self, workspace_id: UUID, project_id: UUID, task_id: UUID) -> NativeTask | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM native_tasks WHERE workspace_id=%s AND project_id=%s AND id=%s",
                (workspace_id, project_id, task_id),
            ).fetchone()
            return self._native_task(row) if row else None

    def native_tasks(
        self, workspace_id: UUID, project_id: UUID, offset: int, limit: int
    ) -> tuple[NativeTask, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM native_tasks WHERE workspace_id=%s AND project_id=%s "
                "ORDER BY created_at,id LIMIT %s OFFSET %s",
                (workspace_id, project_id, limit, offset),
            ).fetchall()
            return tuple(self._native_task(row) for row in rows)

    def insert_native_task(self, task: NativeTask) -> None:
        if task.version != 1:
            raise InvalidTransitionError("Native tasks must start at version one")
        try:
            with self.transaction():
                result = self.connection.execute(
                    "INSERT INTO native_tasks (id,workspace_id,project_id,title,description,status,"
                    "assignment_kind,assignee_actor_id,assignee_agent_id,created_by,"
                    "created_by_agent_id,version,created_at,updated_at) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
                    "WHERE %s::uuid IS NULL OR EXISTS ("
                    "SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s)",
                    (
                        task.id,
                        task.workspace_id,
                        task.project_id,
                        task.title,
                        task.description,
                        task.status,
                        task.assignment.kind,
                        task.assignment.actor_id,
                        task.assignment.agent_id,
                        task.created_by,
                        task.created_by_agent_id,
                        task.version,
                        task.created_at,
                        task.updated_at,
                        task.created_by,
                        task.workspace_id,
                        task.created_by,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Native task creator is unavailable")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Native task exists or project/assignee is unavailable"
            ) from exc

    def update_native_task(self, task: NativeTask, expected_version: int) -> None:
        if task.version != expected_version + 1:
            raise InvalidTransitionError("Native task update must advance the version")
        try:
            with self.transaction():
                result = self.connection.execute(
                    "UPDATE native_tasks SET title=%s,description=%s,status=%s,assignment_kind=%s,"
                    "assignee_actor_id=%s,assignee_agent_id=%s,version=%s,updated_at=%s "
                    "WHERE workspace_id=%s AND project_id=%s AND id=%s AND version=%s "
                    "AND created_by IS NOT DISTINCT FROM %s "
                    "AND created_by_agent_id IS NOT DISTINCT FROM %s AND created_at=%s",
                    (
                        task.title,
                        task.description,
                        task.status,
                        task.assignment.kind,
                        task.assignment.actor_id,
                        task.assignment.agent_id,
                        task.version,
                        task.updated_at,
                        task.workspace_id,
                        task.project_id,
                        task.id,
                        expected_version,
                        task.created_by,
                        task.created_by_agent_id,
                        task.created_at,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Native task missing or stale task version")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError("Native task project or assignee is unavailable") from exc

    def native_agent(
        self, workspace_id: UUID, project_id: UUID, agent_id: UUID
    ) -> NativeAgent | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM native_agents WHERE workspace_id=%s AND project_id=%s AND id=%s",
                (workspace_id, project_id, agent_id),
            ).fetchone()
            return NativeAgent.model_validate(row) if row else None

    def native_agents(
        self, workspace_id: UUID, project_id: UUID, offset: int, limit: int
    ) -> tuple[NativeAgent, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM native_agents WHERE workspace_id=%s AND project_id=%s "
                "ORDER BY created_at,id LIMIT %s OFFSET %s",
                (workspace_id, project_id, limit, offset),
            ).fetchall()
            return tuple(NativeAgent.model_validate(row) for row in rows)

    def native_active_agent_count(self, workspace_id: UUID, project_id: UUID) -> int:
        with self.transaction():
            row = self.connection.execute(
                "SELECT count(*) AS total FROM native_agents "
                "WHERE workspace_id=%s AND project_id=%s AND status='active'",
                (workspace_id, project_id),
            ).fetchone()
            return int(row["total"]) if row else 0

    def insert_native_agent(self, agent: NativeAgent) -> None:
        if agent.version != 1:
            raise InvalidTransitionError("Native agents must start at version one")
        try:
            with self.transaction():
                result = self.connection.execute(
                    "INSERT INTO native_agents (id,workspace_id,project_id,name,role_key,"
                    "instructions,success_criteria,rationale,status,can_manage_team,created_by,"
                    "created_by_agent_id,version,created_at,updated_at) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
                    "WHERE %s::uuid IS NULL OR EXISTS (SELECT 1 FROM memberships "
                    "WHERE workspace_id=%s AND user_id=%s)",
                    (
                        agent.id,
                        agent.workspace_id,
                        agent.project_id,
                        agent.name,
                        agent.role_key,
                        agent.instructions,
                        agent.success_criteria,
                        agent.rationale,
                        agent.status,
                        agent.can_manage_team,
                        agent.created_by,
                        agent.created_by_agent_id,
                        agent.version,
                        agent.created_at,
                        agent.updated_at,
                        agent.created_by,
                        agent.workspace_id,
                        agent.created_by,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Native agent creator is unavailable")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Agent role exists or project/creator is unavailable"
            ) from exc

    def update_native_agent(self, agent: NativeAgent, expected_version: int) -> None:
        if agent.version != expected_version + 1:
            raise InvalidTransitionError("Native agent update must advance the version")
        with self.transaction():
            result = self.connection.execute(
                "UPDATE native_agents SET name=%s,instructions=%s,success_criteria=%s,rationale=%s,"
                "status=%s,can_manage_team=%s,version=%s,updated_at=%s "
                "WHERE workspace_id=%s AND project_id=%s AND id=%s AND version=%s AND role_key=%s "
                "AND created_by IS NOT DISTINCT FROM %s "
                "AND created_by_agent_id IS NOT DISTINCT FROM %s AND created_at=%s",
                (
                    agent.name,
                    agent.instructions,
                    agent.success_criteria,
                    agent.rationale,
                    agent.status,
                    agent.can_manage_team,
                    agent.version,
                    agent.updated_at,
                    agent.workspace_id,
                    agent.project_id,
                    agent.id,
                    expected_version,
                    agent.role_key,
                    agent.created_by,
                    agent.created_by_agent_id,
                    agent.created_at,
                ),
            )
            if result.rowcount != 1:
                raise InvalidTransitionError("Native agent missing or stale agent version")

    def native_team_policy(self, workspace_id: UUID, project_id: UUID) -> NativeTeamPolicy | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM native_team_policies WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            ).fetchone()
            return NativeTeamPolicy.model_validate(row) if row else None

    def save_native_team_policy(self, policy: NativeTeamPolicy, expected_version: int) -> None:
        if policy.version != expected_version + 1:
            raise InvalidTransitionError("Native team policy must advance the version")
        try:
            with self.transaction():
                if expected_version == 0:
                    result = self.connection.execute(
                        "INSERT INTO native_team_policies "
                        "(workspace_id,project_id,max_active_agents,"
                        "agents_can_manage_team,version,updated_at) VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (workspace_id,project_id) DO NOTHING",
                        (
                            policy.workspace_id,
                            policy.project_id,
                            policy.max_active_agents,
                            policy.agents_can_manage_team,
                            policy.version,
                            policy.updated_at,
                        ),
                    )
                else:
                    result = self.connection.execute(
                        "UPDATE native_team_policies SET max_active_agents=%s,"
                        "agents_can_manage_team=%s,version=%s,updated_at=%s "
                        "WHERE workspace_id=%s AND project_id=%s AND version=%s",
                        (
                            policy.max_active_agents,
                            policy.agents_can_manage_team,
                            policy.version,
                            policy.updated_at,
                            policy.workspace_id,
                            policy.project_id,
                            expected_version,
                        ),
                    )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Native team policy missing or stale version")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError("Native team policy project is unavailable") from exc

    def native_agent_credential(self, token_id: UUID) -> NativeAgentCredential | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM native_agent_credentials WHERE id=%s", (token_id,)
            ).fetchone()
            return NativeAgentCredential.model_validate(row) if row else None

    def native_agent_credentials(
        self, workspace_id: UUID, project_id: UUID, agent_id: UUID
    ) -> tuple[NativeAgentCredential, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM native_agent_credentials "
                "WHERE workspace_id=%s AND project_id=%s AND agent_id=%s ORDER BY created_at,id",
                (workspace_id, project_id, agent_id),
            ).fetchall()
            return tuple(NativeAgentCredential.model_validate(row) for row in rows)

    def insert_native_agent_credential(self, credential: NativeAgentCredential) -> None:
        try:
            with self.transaction():
                result = self.connection.execute(
                    "INSERT INTO native_agent_credentials (id,workspace_id,project_id,agent_id,"
                    "agent_version,token_hash,scopes,issued_by,expires_at,revoked_at,created_at) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE EXISTS ("
                    "SELECT 1 FROM native_agents WHERE workspace_id=%s AND project_id=%s "
                    "AND id=%s AND version=%s AND status='active') AND EXISTS ("
                    "SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s)",
                    (
                        credential.id,
                        credential.workspace_id,
                        credential.project_id,
                        credential.agent_id,
                        credential.agent_version,
                        credential.token_hash,
                        sorted(credential.scopes),
                        credential.issued_by,
                        credential.expires_at,
                        credential.revoked_at,
                        credential.created_at,
                        credential.workspace_id,
                        credential.project_id,
                        credential.agent_id,
                        credential.agent_version,
                        credential.workspace_id,
                        credential.issued_by,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Agent credential authority is unavailable")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Agent credential exists or authority is unavailable"
            ) from exc

    def revoke_native_agent_credential(
        self,
        workspace_id: UUID,
        project_id: UUID,
        agent_id: UUID,
        token_id: UUID,
        revoked_at: datetime,
    ) -> bool:
        try:
            with self.transaction():
                result = self.connection.execute(
                    "UPDATE native_agent_credentials SET revoked_at=%s WHERE workspace_id=%s "
                    "AND project_id=%s AND agent_id=%s AND id=%s AND revoked_at IS NULL",
                    (revoked_at, workspace_id, project_id, agent_id, token_id),
                )
                return result.rowcount == 1
        except psycopg.errors.CheckViolation as exc:
            raise InvalidTransitionError("Credential revocation predates issuance") from exc

    def native_release_agent_tasks(
        self, workspace_id: UUID, project_id: UUID, agent_id: UUID
    ) -> tuple[NativeTask, ...]:
        with self.transaction(workspace_id):
            rows = self.connection.execute(
                "UPDATE native_tasks SET assignment_kind='pool',assignee_agent_id=NULL,"
                "status=CASE WHEN status='in_progress' THEN 'todo' ELSE status END,"
                "version=version+1,updated_at=now() WHERE workspace_id=%s AND project_id=%s "
                "AND assignee_agent_id=%s AND status NOT IN ('done','cancelled') RETURNING *",
                (workspace_id, project_id, agent_id),
            ).fetchall()
            return tuple(
                sorted((self._native_task(row) for row in rows), key=lambda t: (t.created_at, t.id))
            )

    def native_intake(self, workspace_id: UUID, project_id: UUID) -> NativeIntake | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM native_intakes WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            ).fetchone()
            return NativeIntake.model_validate(row["snapshot"]) if row else None

    def save_native_intake(self, value: NativeIntake, expected_version: int) -> None:
        if value.version != expected_version + 1:
            raise InvalidTransitionError("Native intake must advance the version")
        try:
            with self.transaction():
                if expected_version == 0:
                    result = self.connection.execute(
                        "INSERT INTO native_intakes (workspace_id,project_id,version,snapshot) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id,project_id) DO NOTHING",
                        (
                            value.workspace_id,
                            value.project_id,
                            value.version,
                            Jsonb(value.model_dump(mode="json")),
                        ),
                    )
                else:
                    result = self.connection.execute(
                        "UPDATE native_intakes SET version=%s,snapshot=%s "
                        "WHERE workspace_id=%s AND project_id=%s AND version=%s",
                        (
                            value.version,
                            Jsonb(value.model_dump(mode="json")),
                            value.workspace_id,
                            value.project_id,
                            expected_version,
                        ),
                    )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Native intake missing or stale version")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError("Native intake project is unavailable") from exc

    def native_intake_sources(
        self, workspace_id: UUID, project_id: UUID
    ) -> tuple[IntakeSource, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM native_intake_sources "
                "WHERE workspace_id=%s AND project_id=%s "
                "ORDER BY created_at,id",
                (workspace_id, project_id),
            ).fetchall()
            return tuple(IntakeSource.model_validate(row["snapshot"]) for row in rows)

    def native_intake_source(
        self, workspace_id: UUID, project_id: UUID, source_id: UUID
    ) -> IntakeSource | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM native_intake_sources "
                "WHERE workspace_id=%s AND project_id=%s AND id=%s",
                (workspace_id, project_id, source_id),
            ).fetchone()
            return IntakeSource.model_validate(row["snapshot"]) if row else None

    def insert_native_intake_source(self, value: IntakeSource) -> None:
        try:
            with self.transaction():
                result = self.connection.execute(
                    "INSERT INTO native_intake_sources "
                    "(id,workspace_id,project_id,source_key,revision,sha256,size_bytes,"
                    "created_by,created_at,revoked_at,snapshot) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE EXISTS "
                    "(SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s)",
                    (
                        value.id,
                        value.workspace_id,
                        value.project_id,
                        value.source_key,
                        value.revision,
                        value.sha256,
                        value.size_bytes,
                        value.created_by,
                        value.created_at,
                        value.revoked_at,
                        Jsonb(value.model_dump(mode="json")),
                        value.workspace_id,
                        value.created_by,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Intake source creator is unavailable")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Intake source exists or its project/creator is unavailable"
            ) from exc

    def revoke_native_intake_source(
        self, workspace_id: UUID, project_id: UUID, source_id: UUID, at: datetime
    ) -> bool:
        try:
            with self.transaction():
                result = self.connection.execute(
                    "UPDATE native_intake_sources SET revoked_at=%s,"
                    "snapshot=jsonb_set(snapshot,'{revoked_at}',to_jsonb(%s::text)) "
                    "WHERE workspace_id=%s AND project_id=%s AND id=%s AND revoked_at IS NULL",
                    (at, at.isoformat(), workspace_id, project_id, source_id),
                )
                return result.rowcount == 1
        except psycopg.errors.CheckViolation as exc:
            raise InvalidTransitionError("Source revocation predates creation") from exc

    def native_intake_runs(self, workspace_id: UUID, project_id: UUID) -> tuple[IntakeRun, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM native_intake_runs WHERE workspace_id=%s AND project_id=%s "
                "ORDER BY started_at DESC,id DESC",
                (workspace_id, project_id),
            ).fetchall()
            return tuple(IntakeRun.model_validate(row["snapshot"]) for row in rows)

    def native_intake_run(
        self, workspace_id: UUID, project_id: UUID, run_id: UUID
    ) -> IntakeRun | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM native_intake_runs "
                "WHERE workspace_id=%s AND project_id=%s AND id=%s",
                (workspace_id, project_id, run_id),
            ).fetchone()
            return IntakeRun.model_validate(row["snapshot"]) if row else None

    def native_intake_run_by_key(
        self, workspace_id: UUID, project_id: UUID, key: str
    ) -> IntakeRun | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM native_intake_runs "
                "WHERE workspace_id=%s AND project_id=%s AND idempotency_key=%s",
                (workspace_id, project_id, key),
            ).fetchone()
            return IntakeRun.model_validate(row["snapshot"]) if row else None

    def insert_native_intake_run(self, value: IntakeRun) -> None:
        if value.version != 1:
            raise InvalidTransitionError("New intake runs must start at version one")
        try:
            with self.transaction():
                result = self.connection.execute(
                    "INSERT INTO native_intake_runs "
                    "(id,workspace_id,project_id,requested_by,idempotency_key,"
                    "request_digest,intake_version,version,status,reserved_microusd,charged_microusd,"
                    "started_at,deadline_at,finished_at,snapshot) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE EXISTS "
                    "(SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s) AND EXISTS "
                    "(SELECT 1 FROM native_intakes "
                    "WHERE workspace_id=%s AND project_id=%s AND version=%s)",
                    (
                        value.id,
                        value.workspace_id,
                        value.project_id,
                        value.requested_by,
                        value.idempotency_key,
                        value.request_digest,
                        value.intake_version,
                        value.version,
                        value.status,
                        value.reserved_microusd,
                        value.charged_microusd,
                        value.started_at,
                        value.deadline_at,
                        value.finished_at,
                        Jsonb(value.model_dump(mode="json")),
                        value.workspace_id,
                        value.requested_by,
                        value.workspace_id,
                        value.project_id,
                        value.intake_version,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError("Intake context/requester is unavailable")
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError(
                "Intake run exists or context/requester is unavailable"
            ) from exc

    def update_native_intake_run(self, value: IntakeRun, expected_version: int) -> None:
        if value.version != expected_version + 1:
            raise InvalidTransitionError("Intake run must advance the version")
        mutable = sorted(INTAKE_RUN_MUTABLE_FIELDS)
        snapshot = Jsonb(value.model_dump(mode="json"))
        try:
            with self.transaction():
                result = self.connection.execute(
                    "UPDATE native_intake_runs SET version=%s,status=%s,reserved_microusd=%s,"
                    "charged_microusd=%s,finished_at=%s,snapshot=%s "
                    "WHERE workspace_id=%s AND project_id=%s AND id=%s AND version=%s "
                    "AND snapshot - %s::text[] = %s::jsonb - %s::text[]",
                    (
                        value.version,
                        value.status,
                        value.reserved_microusd,
                        value.charged_microusd,
                        value.finished_at,
                        snapshot,
                        value.workspace_id,
                        value.project_id,
                        value.id,
                        expected_version,
                        mutable,
                        snapshot,
                        mutable,
                    ),
                )
                if result.rowcount != 1:
                    raise InvalidTransitionError(
                        "Intake run missing, stale or immutable context changed"
                    )
        except psycopg.IntegrityError as exc:
            raise InvalidTransitionError("Intake run context is unavailable") from exc

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
                "INSERT INTO managed_accounts (actor_id, workspace_id, invited_by, snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (actor_id) "
                "DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    account.actor_id,
                    account.workspace_id,
                    account.invited_by,
                    Jsonb(account.model_dump(mode="json")),
                ),
            )

    def voice_sessions(self, workspace_id: UUID, actor_id: UUID) -> tuple[VoiceSession, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM voice_sessions WHERE workspace_id = %s AND actor_id = %s "
                "ORDER BY created_at DESC LIMIT 50",
                (workspace_id, actor_id),
            ).fetchall()
            return tuple(VoiceSession.model_validate(row["snapshot"]) for row in rows)

    def voice_session(self, workspace_id: UUID, session_id: UUID) -> VoiceSession | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM voice_sessions WHERE workspace_id = %s AND id = %s",
                (workspace_id, session_id),
            ).fetchone()
            return VoiceSession.model_validate(row["snapshot"]) if row else None

    def save_voice_session(self, session: VoiceSession) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO voice_sessions (id, workspace_id, actor_id, thread_id, created_at, "
                "snapshot) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE "
                "SET snapshot = EXCLUDED.snapshot",
                (
                    session.id,
                    session.workspace_id,
                    session.actor_id,
                    session.thread_id,
                    session.created_at,
                    Jsonb(session.model_dump(mode="json")),
                ),
            )

    def response_preferences(
        self, workspace_id: UUID, actor_id: UUID
    ) -> ResponsePreferences | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM response_preferences "
                "WHERE workspace_id = %s AND actor_id = %s",
                (workspace_id, actor_id),
            ).fetchone()
            return ResponsePreferences.model_validate(row["snapshot"]) if row else None

    def save_response_preferences(
        self, workspace_id: UUID, actor_id: UUID, preferences: ResponsePreferences
    ) -> None:
        with self.transaction(workspace_id):
            self.connection.execute(
                "INSERT INTO response_preferences (workspace_id, actor_id, snapshot) "
                "VALUES (%s,%s,%s) ON CONFLICT (workspace_id, actor_id) DO UPDATE "
                "SET snapshot = EXCLUDED.snapshot",
                (workspace_id, actor_id, Jsonb(preferences.model_dump(mode="json"))),
            )

    def feedback(self, workspace_id: UUID, actor_id: UUID, run_id: UUID) -> RunFeedback | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT snapshot FROM run_feedback "
                "WHERE workspace_id = %s AND actor_id = %s AND run_id = %s",
                (workspace_id, actor_id, run_id),
            ).fetchone()
            return RunFeedback.model_validate(row["snapshot"]) if row else None

    def save_feedback(self, workspace_id: UUID, actor_id: UUID, feedback: RunFeedback) -> None:
        with self.transaction(workspace_id):
            self.connection.execute(
                "INSERT INTO run_feedback (workspace_id, actor_id, run_id, snapshot) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id, actor_id, run_id) DO UPDATE "
                "SET snapshot = EXCLUDED.snapshot",
                (workspace_id, actor_id, feedback.run_id, Jsonb(feedback.model_dump(mode="json"))),
            )

    def answer_runs(self, thread_id: UUID, offset: int, limit: int) -> tuple[Run, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT r.snapshot FROM runs r LEFT JOIN messages m "
                "ON m.id = (r.snapshot->>'output_message_id')::uuid AND m.thread_id = r.thread_id "
                "WHERE r.thread_id = %s AND r.snapshot IS NOT NULL "
                "ORDER BY COALESCE(m.sequence, 0), r.created_at, r.id OFFSET %s LIMIT %s",
                (thread_id, offset, limit),
            ).fetchall()
            return tuple(Run.model_validate(row["snapshot"]) for row in rows)

    def latest_run(self, thread_id: UUID) -> Run | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT r.snapshot FROM runs r LEFT JOIN messages m "
                "ON m.id = (r.snapshot->>'output_message_id')::uuid AND m.thread_id = r.thread_id "
                "WHERE r.thread_id = %s AND r.snapshot IS NOT NULL "
                "ORDER BY COALESCE(m.sequence, 0) DESC, r.created_at DESC, r.id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            return Run.model_validate(row["snapshot"]) if row else None

    def save_attempt(self, attempt: ModelAttempt) -> None:
        with self.transaction(attempt.workspace_id):
            self.connection.execute(
                "INSERT INTO model_attempts (id, workspace_id, thread_id, status, snapshot) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE "
                "SET status = EXCLUDED.status, snapshot = EXCLUDED.snapshot",
                (
                    attempt.run.id,
                    attempt.workspace_id,
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
        with self.transaction(memory.workspace_id):
            self.connection.execute(
                "INSERT INTO memories (id, workspace_id, subject, scope, kind, content, source, "
                "confidence, sensitivity, review_status, created_at, explicit_snapshot) "
                "VALUES (%s,%s,%s,%s,'explicit',%s,%s,1,'personal','accepted',%s,%s)",
                (
                    memory.id,
                    memory.workspace_id,
                    memory.subject,
                    memory.scope,
                    memory.content,
                    Jsonb({"type": memory.source, "actor_id": str(memory.created_by)}),
                    memory.created_at,
                    Jsonb(memory.model_dump(mode="json")),
                ),
            )

    def update_project_memory(
        self, previous: ExplicitMemory, name: str, description: str
    ) -> ExplicitMemory:
        with self.transaction(previous.workspace_id):
            row = self.connection.execute(
                "SELECT explicit_snapshot FROM memories WHERE workspace_id = %s AND id = %s "
                "AND review_status = 'accepted' AND explicit_snapshot IS NOT NULL FOR UPDATE",
                (previous.workspace_id, previous.id),
            ).fetchone()
            if (
                row is None
                or ExplicitMemory.model_validate(row["explicit_snapshot"]) != previous
                or not previous.accepted
                or previous.category != "project"
            ):
                raise InvalidTransitionError("Project details changed before saving")
            updated = previous.model_copy(update={"subject": name, "content": description})
            self.connection.execute(
                "UPDATE memories SET subject = %s, content = %s, explicit_snapshot = %s "
                "WHERE workspace_id = %s AND id = %s",
                (
                    name,
                    description,
                    Jsonb(updated.model_dump(mode="json")),
                    previous.workspace_id,
                    previous.id,
                ),
            )
            return updated

    def explicit_memories(
        self,
        workspace_id: UUID,
        offset: int,
        limit: int,
        actor_id: UUID | None = None,
        personal: bool = True,
    ) -> tuple[ExplicitMemory, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT explicit_snapshot FROM memories WHERE workspace_id = %s "
                "AND explicit_snapshot IS NOT NULL AND review_status = 'accepted' "
                "AND (scope = 'workspace' OR (%s AND (%s::uuid IS NULL OR "
                "explicit_snapshot->>'created_by' = %s))) "
                "ORDER BY created_at, id LIMIT %s OFFSET %s",
                (workspace_id, personal, actor_id, str(actor_id), limit, offset),
            ).fetchall()
            return tuple(ExplicitMemory.model_validate(row["explicit_snapshot"]) for row in rows)

    def explicit_memory(self, workspace_id: UUID, memory_id: UUID) -> ExplicitMemory | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT explicit_snapshot FROM memories WHERE workspace_id = %s AND id = %s "
                "AND explicit_snapshot IS NOT NULL",
                (workspace_id, memory_id),
            ).fetchone()
            return ExplicitMemory.model_validate(row["explicit_snapshot"]) if row else None

    def retract_memory(self, memory: ExplicitMemory) -> None:
        with self.transaction(memory.workspace_id):
            self.connection.execute(
                "UPDATE memories SET review_status = 'rejected', explicit_snapshot = %s "
                "WHERE workspace_id = %s AND id = %s",
                (
                    Jsonb(memory.model_copy(update={"accepted": False}).model_dump(mode="json")),
                    memory.workspace_id,
                    memory.id,
                ),
            )

    def insert_thread(self, thread: Thread) -> None:
        with self.transaction(thread.workspace_id):
            self.connection.execute(
                "INSERT INTO threads (id, workspace_id, created_by, title, created_at, visibility) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    thread.id,
                    thread.workspace_id,
                    thread.created_by,
                    thread.title,
                    thread.created_at,
                    thread.visibility,
                ),
            )

    def threads(
        self, workspace_id: UUID, offset: int, limit: int, actor_id: UUID | None = None
    ) -> tuple[Thread, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT id, workspace_id, created_by, title, created_at, visibility FROM threads "
                "WHERE workspace_id = %s AND (%s::uuid IS NULL OR visibility = 'workspace' "
                "OR created_by = %s) ORDER BY created_at, id LIMIT %s OFFSET %s",
                (workspace_id, actor_id, actor_id, limit, offset),
            ).fetchall()
            return tuple(Thread.model_validate(row) for row in rows)

    def thread(self, workspace_id: UUID, thread_id: UUID) -> Thread | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT id, workspace_id, created_by, title, created_at, visibility FROM threads "
                "WHERE workspace_id = %s AND id = %s",
                (workspace_id, thread_id),
            ).fetchone()
            return Thread.model_validate(row) if row else None

    def recall_documents(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        terms: tuple[str, ...],
        offset: int,
        limit: int,
        exclude_thread: UUID | None = None,
    ) -> tuple[RecallDocument, ...]:
        with self.transaction():
            rows = self.connection.execute(
                """WITH owned AS (
                    SELECT * FROM threads WHERE workspace_id = %(workspace)s
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
                    WHERE v.workspace_id = %(workspace)s AND v.actor_id = %(actor)s
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
                    "workspace": workspace_id,
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
        with self.transaction(job.workspace_id):
            row = self.connection.execute(
                "INSERT INTO jobs (id, workspace_id, created_by, kind, schema_version, "
                "idempotency_key, input, input_digest, status, version, created_at, updated_at, "
                "result, error_code) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (workspace_id, kind, idempotency_key) DO NOTHING RETURNING *",
                (
                    job.id,
                    job.workspace_id,
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
                    Jsonb(job.result) if job.result is not None else None,
                    job.error_code,
                ),
            ).fetchone()
            if row is not None:
                return self._job(row), True
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE workspace_id = %s AND kind = %s AND idempotency_key = %s",
                (job.workspace_id, job.kind, job.idempotency_key),
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
        self, workspace_id: UUID, actor_id: UUID, kind: str, offset: int, limit: int
    ) -> tuple[Job, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE workspace_id=%s AND created_by=%s AND kind=%s "
                "ORDER BY COALESCE((input->>'priority')::int,3) DESC, "
                "COALESCE((input->>'rank')::bigint,0), created_at, id OFFSET %s LIMIT %s",
                (workspace_id, actor_id, kind, offset, limit),
            ).fetchall()
            return tuple(self._job(row) for row in rows)

    def project_run_jobs(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        before: tuple[datetime, UUID] | None,
        limit: int,
    ) -> tuple[Job, ...]:
        cursor_clause = " AND (r.created_at,r.id) < (%s,%s)" if before else ""
        parameters: tuple[Any, ...] = (workspace_id, actor_id, str(project_id))
        if before:
            parameters += before
        with self.transaction():
            rows = self.connection.execute(
                "SELECT r.* FROM jobs p JOIN jobs r ON "
                "r.workspace_id=p.workspace_id AND r.created_by=p.created_by "
                "AND r.input->>'plan_id'=p.id::text "
                "WHERE p.workspace_id=%s AND p.created_by=%s "
                "AND p.kind='platform.plan' AND r.kind='platform.run' "
                "AND p.input->'plan'->>'project_id'=%s"
                + cursor_clause
                + " ORDER BY r.created_at DESC,r.id DESC LIMIT %s",
                (*parameters, limit),
            ).fetchall()
            return tuple(self._job(row) for row in rows)

    def project_activity_jobs(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        query: str,
        activity_kind: str | None,
        before_sequence: int | None,
        limit: int,
    ) -> tuple[Job, ...]:
        clauses = [
            "workspace_id=%s",
            "created_by=%s",
            "kind=%s",
            "kind LIKE 'platform.project_activity.%%'",
        ]
        parameters: list[Any] = [
            workspace_id,
            actor_id,
            "platform.project_activity." + project_id.hex,
        ]
        if query:
            # strpos uses a literal substring: '%' and '_' never become wildcards.
            clauses.append("strpos(lower(input->'initial_state'->>'text'),lower(%s)) > 0")
            parameters.append(query)
        if activity_kind is not None:
            clauses.append("input->'initial_state'->>'kind'=%s")
            parameters.append(activity_kind)
        if before_sequence is not None:
            clauses.append("(input->'initial_state'->>'sequence')::bigint < %s")
            parameters.append(before_sequence)
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY (input->'initial_state'->>'sequence')::bigint DESC LIMIT %s",
                (*parameters, limit),
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
        with self.transaction(event.workspace_id):
            row = self.connection.execute(
                "SELECT sequence, event_hash FROM audit_events WHERE workspace_id = %s "
                "ORDER BY sequence DESC LIMIT 1",
                (event.workspace_id,),
            ).fetchone()
            if event.sequence != (row["sequence"] + 1 if row else 1):
                raise ValueError("audit sequence is not contiguous")
            if event.previous_hash != (row["event_hash"] if row else "0" * 64):
                raise ValueError("audit hash chain is invalid")
            self.connection.execute(
                "INSERT INTO audit_events (id, workspace_id, sequence, occurred_at, event_type, "
                "actor_id, correlation_id, resource_type, resource_id, payload, previous_hash, "
                "event_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.id,
                    event.workspace_id,
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

    def audit_events(self, workspace_id: UUID | None = None) -> tuple[AuditEvent, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM audit_events WHERE (%s::uuid IS NULL OR workspace_id = %s) "
                "ORDER BY workspace_id, sequence",
                (workspace_id, workspace_id),
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
                "SELECT m.user_id AS actor_id, m.workspace_id, m.role, u.display_name, "
                "h.name AS workspace_name FROM memberships m JOIN users u ON u.id = m.user_id "
                "JOIN workspaces h ON h.id = m.workspace_id WHERE m.user_id = %s "
                "ORDER BY m.workspace_id",
                (actor_id,),
            )
            return tuple(Membership.model_validate(row) for row in rows)

    def workspace_members(
        self, workspace_id: UUID, offset: int, limit: int
    ) -> tuple[Membership, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT m.user_id AS actor_id, m.workspace_id, m.role, u.display_name, "
                "w.name AS workspace_name FROM memberships m JOIN users u ON u.id=m.user_id "
                "JOIN workspaces w ON w.id=m.workspace_id WHERE m.workspace_id=%s "
                "ORDER BY m.user_id LIMIT %s OFFSET %s",
                (workspace_id, limit, offset),
            )
            return tuple(Membership.model_validate(row) for row in rows)

    def put_membership(self, membership: Membership) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO users (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (membership.actor_id, membership.display_name),
            )
            self.connection.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (membership.workspace_id, membership.workspace_name),
            )
            self.connection.execute(
                "INSERT INTO memberships (user_id, workspace_id, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role",
                (membership.actor_id, membership.workspace_id, membership.role),
            )

    def delete_membership(self, actor_id: UUID, workspace_id: UUID) -> None:
        with self.transaction(workspace_id):
            # Revocation must preserve shared work and cannot depend on manual reassignment.
            self.connection.execute(
                "UPDATE native_projects p SET version=p.version+1,updated_at=now() "
                "WHERE p.workspace_id=%s AND EXISTS (SELECT 1 FROM native_project_members m "
                "WHERE m.workspace_id=p.workspace_id AND m.project_id=p.id AND m.actor_id=%s)",
                (workspace_id, actor_id),
            )
            self.connection.execute(
                "UPDATE native_tasks SET assignment_kind='pool',assignee_actor_id=NULL,"
                "status=CASE WHEN status='in_progress' THEN 'todo' ELSE status END,"
                "version=version+1,updated_at=now() WHERE workspace_id=%s AND assignee_actor_id=%s",
                (workspace_id, actor_id),
            )
            self.connection.execute(
                "DELETE FROM native_project_members WHERE workspace_id=%s AND actor_id=%s",
                (workspace_id, actor_id),
            )
            self.connection.execute(
                "DELETE FROM memberships WHERE user_id = %s AND workspace_id = %s",
                (actor_id, workspace_id),
            )

    def save_session(self, session: Session) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
            self.connection.execute(
                "INSERT INTO auth_sessions (token_hash, actor_id, workspace_id, method, "
                "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    session.token_hash,
                    session.actor_id,
                    session.workspace_id,
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
                "INSERT INTO auth_enrollments (token_hash, actor_id, workspace_id, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    enrollment.token_hash,
                    enrollment.actor_id,
                    enrollment.workspace_id,
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
                "workspace_id, enrollment_hash, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    challenge.token_hash,
                    challenge.binding_hash,
                    challenge.challenge,
                    challenge.kind,
                    challenge.actor_id,
                    challenge.workspace_id,
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
                "(actor_id, username, password_hash, failed_attempts, locked_until, email) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (actor_id) DO UPDATE SET "
                "username = EXCLUDED.username, password_hash = EXCLUDED.password_hash, "
                "failed_attempts = EXCLUDED.failed_attempts, locked_until = EXCLUDED.locked_until, "
                "email = EXCLUDED.email",
                (
                    credential.actor_id,
                    credential.username,
                    credential.password_hash,
                    credential.failed_attempts,
                    credential.locked_until,
                    credential.email,
                ),
            )

    def password_for_email(self, email: str) -> PasswordCredential | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_passwords WHERE email=%s", (email,)
            ).fetchone()
            return PasswordCredential.model_validate(row) if row else None

    def email_code(self, identifier: UUID) -> EmailCode | None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM auth_email_codes WHERE id=%s", (identifier,)
            ).fetchone()
            return EmailCode.model_validate(row) if row else None

    def email_codes(
        self, email: str, purpose: str, since: datetime, *, actor_id: UUID | None = None
    ) -> tuple[EmailCode, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM auth_email_codes WHERE (email=%s OR actor_id=%s) "
                "AND purpose=%s AND created_at >= %s ORDER BY created_at DESC",
                (email, actor_id, purpose, since),
            ).fetchall()
            return tuple(EmailCode.model_validate(row) for row in rows)

    def save_email_code(self, code: EmailCode) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO auth_email_codes "
                "(id,actor_id,workspace_id,email,purpose,code_hash,created_at,"
                "expires_at,attempts,consumed) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET attempts=EXCLUDED.attempts,"
                "consumed=EXCLUDED.consumed,expires_at=EXCLUDED.expires_at",
                (
                    code.id,
                    code.actor_id,
                    code.workspace_id,
                    code.email,
                    code.purpose,
                    code.code_hash,
                    code.created_at,
                    code.expires_at,
                    code.attempts,
                    code.consumed,
                ),
            )

    def purge_email_codes(self, before: datetime) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM auth_email_codes WHERE expires_at < %s", (before,))

    def integration_connections(
        self, workspace_id: UUID, actor_id: UUID
    ) -> tuple[IntegrationConnection, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM integration_connections "
                "WHERE workspace_id=%s AND actor_id=%s ORDER BY id",
                (workspace_id, actor_id),
            ).fetchall()
            return tuple(IntegrationConnection.model_validate(row["snapshot"]) for row in rows)

    def save_integration_connection(self, connection: IntegrationConnection) -> None:
        with self.transaction(connection.workspace_id):
            self.connection.execute(
                "INSERT INTO integration_connections (workspace_id, actor_id, id, snapshot) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (workspace_id, actor_id, id) DO UPDATE SET snapshot=EXCLUDED.snapshot",
                (
                    connection.workspace_id,
                    connection.actor_id,
                    connection.id,
                    Jsonb(connection.model_dump(mode="json")),
                ),
            )

    def delete_integration_connection(
        self, workspace_id: UUID, actor_id: UUID, identifier: str
    ) -> None:
        with self.transaction(workspace_id):
            self.connection.execute(
                "DELETE FROM integration_connections "
                "WHERE workspace_id=%s AND actor_id=%s AND id=%s",
                (workspace_id, actor_id, identifier),
            )

    def google_connections(
        self, workspace_id: UUID, actor_id: UUID
    ) -> tuple[GoogleConnection, ...]:
        with self.transaction():
            rows = self.connection.execute(
                "SELECT snapshot FROM google_connections WHERE workspace_id=%s AND actor_id=%s "
                "ORDER BY (snapshot->>'is_default')::boolean DESC, email",
                (workspace_id, actor_id),
            ).fetchall()
            return tuple(GoogleConnection.model_validate(row["snapshot"]) for row in rows)

    def google_connection(self, workspace_id: UUID, actor_id: UUID) -> GoogleConnection | None:
        connections = self.google_connections(workspace_id, actor_id)
        return connections[0] if connections else None

    def save_google_connection(self, connection: GoogleConnection) -> None:
        with self.transaction(connection.workspace_id):
            connections = self.google_connections(connection.workspace_id, connection.actor_id)
            old = next(
                (c for c in connections if c.email.casefold() == connection.email.casefold()), None
            )
            connection = connection.model_copy(
                update={"is_default": old.is_default if old else not connections}
            )
            self.connection.execute(
                "INSERT INTO google_connections (workspace_id, actor_id, email, snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (workspace_id, actor_id, email) "
                "DO UPDATE SET snapshot=EXCLUDED.snapshot",
                (
                    connection.workspace_id,
                    connection.actor_id,
                    connection.email.casefold(),
                    Jsonb(connection.model_dump(mode="json")),
                ),
            )

    def set_default_google_connection(
        self, workspace_id: UUID, actor_id: UUID, connection_id: UUID
    ) -> None:
        with self.transaction(workspace_id):
            connections = self.google_connections(workspace_id, actor_id)
            if not any(c.id == connection_id for c in connections):
                raise NotFoundError("Google account not found.")
            self.connection.execute(
                "UPDATE google_connections SET snapshot=jsonb_set(snapshot, '{is_default}', "
                "to_jsonb((snapshot->>'id')::uuid=%s)) WHERE workspace_id=%s AND actor_id=%s",
                (connection_id, workspace_id, actor_id),
            )

    def delete_google_connection(
        self, workspace_id: UUID, actor_id: UUID, connection_id: UUID | None = None
    ) -> None:
        with self.transaction(workspace_id):
            self.connection.execute(
                "DELETE FROM google_connections WHERE workspace_id=%s AND actor_id=%s "
                "AND (%s::uuid IS NULL OR (snapshot->>'id')::uuid=%s)",
                (workspace_id, actor_id, connection_id, connection_id),
            )
            remaining = self.google_connections(workspace_id, actor_id)
            if remaining and not any(c.is_default for c in remaining):
                self.set_default_google_connection(workspace_id, actor_id, remaining[0].id)

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
                "AND a.workspace_id = t.workspace_id "
                "AND (a.attempt_id IS NOT NULL OR r.snapshot->'action_ids' ? a.id::text) "
                "ORDER BY a.snapshot->>'created_at' DESC, a.id DESC LIMIT %s",
                (thread_id, actor_id, limit),
            ).fetchall()
            return tuple(ActionProposal.model_validate(row["snapshot"]) for row in rows)

    def save_action(self, action: ActionProposal) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO action_proposals "
                "(id, workspace_id, actor_id, run_id, attempt_id, snapshot) "
                "VALUES (%s, %s, %s, "
                "CASE WHEN EXISTS (SELECT 1 FROM runs WHERE id = %s) THEN %s END, "
                "CASE WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id = %s) THEN %s END, %s) "
                "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
                (
                    action.id,
                    action.workspace_id,
                    action.actor_id,
                    action.run_id,
                    action.run_id,
                    action.run_id,
                    action.run_id,
                    Jsonb(action.model_dump(mode="json")),
                ),
            )
