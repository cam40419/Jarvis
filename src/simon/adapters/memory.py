"""Transactional reference store for isolated tests and nonpersistent development."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import BaseModel

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
from simon.domain.models import (
    AuditEvent,
    CapabilityDefinition,
    Job,
    JobStatus,
    OutboxEvent,
    utc_now,
)
from simon.domain.native_agents import NativeAgent, NativeAgentCredential, NativeTeamPolicy
from simon.domain.native_projects import (
    NativeProject,
    NativeProjectMember,
    NativeTask,
    TaskAssignment,
)
from simon.domain.ports import CapabilityHandler
from simon.domain.voice import VoiceSession


class InMemoryStore:
    """Thread-safe reference adapter used for tests and local smoke runs."""

    def close(self) -> None:
        pass

    def __init__(self) -> None:
        self._lock = RLock()
        self._capabilities: dict[
            str, tuple[CapabilityDefinition, type[BaseModel], CapabilityHandler]
        ] = {}
        self._invocations: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._jobs: dict[UUID, Job] = {}
        self._job_keys: dict[tuple[UUID, str, str], UUID] = {}
        self._audit: list[AuditEvent] = []
        self._outbox: list[OutboxEvent] = []
        self._memberships: dict[tuple[UUID, UUID], Membership] = {}
        self._sessions: dict[str, Session] = {}
        self._enrollments: dict[str, Enrollment] = {}
        self._challenges: dict[str, Challenge] = {}
        self._passkeys: dict[str, Passkey] = {}
        self._passwords: dict[UUID, PasswordCredential] = {}
        self._email_codes: dict[UUID, EmailCode] = {}
        self._threads: dict[UUID, Thread] = {}
        self._messages: dict[UUID, Message] = {}
        self._runs: dict[UUID, Run] = {}
        self._run_events: dict[UUID, tuple[RunEvent, ...]] = {}
        self._explicit_memories: dict[UUID, ExplicitMemory] = {}
        self._attempts: dict[UUID, ModelAttempt] = {}
        self._response_preferences: dict[tuple[UUID, UUID], ResponsePreferences] = {}
        self._google: dict[tuple[UUID, UUID, str], GoogleConnection] = {}
        self._integrations: dict[tuple[UUID, UUID, str], IntegrationConnection] = {}
        self._google_states: dict[str, GoogleOAuthState] = {}
        self._actions: dict[UUID, ActionProposal] = {}
        self._feedback: dict[tuple[UUID, UUID, UUID], RunFeedback] = {}
        self._voice_sessions: dict[UUID, VoiceSession] = {}
        self._managed_accounts: dict[UUID, ManagedAccount] = {}
        self._native_projects: dict[UUID, NativeProject] = {}
        self._native_project_members: dict[tuple[UUID, UUID, UUID], NativeProjectMember] = {}
        self._native_tasks: dict[UUID, NativeTask] = {}
        self._native_agents: dict[UUID, NativeAgent] = {}
        self._native_team_policies: dict[tuple[UUID, UUID], NativeTeamPolicy] = {}
        self._native_agent_credentials: dict[UUID, NativeAgentCredential] = {}

    @contextmanager
    def transaction(self, workspace_id: UUID | None = None) -> Iterator[None]:
        with self._lock:
            snapshot = deepcopy(
                (
                    self._invocations,
                    self._jobs,
                    self._job_keys,
                    self._audit,
                    self._outbox,
                    self._memberships,
                    self._sessions,
                    self._enrollments,
                    self._challenges,
                    self._passkeys,
                    self._passwords,
                    self._email_codes,
                    self._threads,
                    self._messages,
                    self._runs,
                    self._run_events,
                    self._explicit_memories,
                    self._attempts,
                    self._response_preferences,
                    self._feedback,
                    self._google,
                    self._integrations,
                    self._google_states,
                    self._actions,
                    self._voice_sessions,
                    self._managed_accounts,
                    self._native_projects,
                    self._native_project_members,
                    self._native_tasks,
                    self._native_agents,
                    self._native_team_policies,
                    self._native_agent_credentials,
                )
            )
            try:
                yield
            except BaseException:
                (
                    self._invocations,
                    self._jobs,
                    self._job_keys,
                    self._audit,
                    self._outbox,
                    self._memberships,
                    self._sessions,
                    self._enrollments,
                    self._challenges,
                    self._passkeys,
                    self._passwords,
                    self._email_codes,
                    self._threads,
                    self._messages,
                    self._runs,
                    self._run_events,
                    self._explicit_memories,
                    self._attempts,
                    self._response_preferences,
                    self._feedback,
                    self._google,
                    self._integrations,
                    self._google_states,
                    self._actions,
                    self._voice_sessions,
                    self._managed_accounts,
                    self._native_projects,
                    self._native_project_members,
                    self._native_tasks,
                    self._native_agents,
                    self._native_team_policies,
                    self._native_agent_credentials,
                ) = snapshot
                raise

    def native_project(self, workspace_id: UUID, project_id: UUID) -> NativeProject | None:
        with self._lock:
            project = self._native_projects.get(project_id)
            return project if project and project.workspace_id == workspace_id else None

    def native_projects(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        offset: int,
        limit: int,
        *,
        all_projects: bool = False,
    ) -> tuple[NativeProject, ...]:
        with self._lock:
            projects = (
                project
                for project in self._native_projects.values()
                if project.workspace_id == workspace_id
                and (
                    all_projects
                    or (workspace_id, project.id, actor_id) in self._native_project_members
                )
            )
            return tuple(
                sorted(projects, key=lambda p: (p.created_at, p.id), reverse=True)[
                    offset : offset + limit
                ]
            )

    def insert_native_project(self, project: NativeProject) -> None:
        with self._lock:
            if (
                project.id in self._native_projects
                or project.version != 1
                or (project.created_by, project.workspace_id) not in self._memberships
            ):
                raise InvalidTransitionError(
                    "Native project already exists or creator is unavailable"
                )
            self._native_projects[project.id] = project

    def update_native_project(self, project: NativeProject, expected_version: int) -> None:
        with self._lock:
            previous = self.native_project(project.workspace_id, project.id)
            if (
                previous is None
                or previous.version != expected_version
                or project.version != expected_version + 1
                or (project.created_by, project.created_at, project.board_authority)
                != (previous.created_by, previous.created_at, previous.board_authority)
            ):
                raise InvalidTransitionError("Native project missing or stale project version")
            self._native_projects[project.id] = project

    def native_project_members(
        self, workspace_id: UUID, project_id: UUID
    ) -> tuple[NativeProjectMember, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        member
                        for member in self._native_project_members.values()
                        if (member.workspace_id, member.project_id) == (workspace_id, project_id)
                    ),
                    key=lambda m: (m.created_at, m.actor_id),
                )
            )

    def native_project_member(
        self, workspace_id: UUID, project_id: UUID, actor_id: UUID
    ) -> NativeProjectMember | None:
        with self._lock:
            return self._native_project_members.get((workspace_id, project_id, actor_id))

    def put_native_project_member(self, member: NativeProjectMember) -> None:
        with self._lock:
            if (
                self.native_project(member.workspace_id, member.project_id) is None
                or (member.actor_id, member.workspace_id) not in self._memberships
            ):
                raise InvalidTransitionError("Native project or workspace member is unavailable")
            key = (member.workspace_id, member.project_id, member.actor_id)
            previous = self._native_project_members.get(key)
            if previous:
                member = member.model_copy(update={"created_at": previous.created_at})
            self._native_project_members[key] = member

    def delete_native_project_member(
        self, workspace_id: UUID, project_id: UUID, actor_id: UUID
    ) -> None:
        with self._lock:
            if self.native_assigned_task_exists(workspace_id, project_id, actor_id):
                raise InvalidTransitionError("Reassign this member's tasks before removing them")
            self._native_project_members.pop((workspace_id, project_id, actor_id), None)

    def native_assigned_task_exists(
        self, workspace_id: UUID, project_id: UUID, actor_id: UUID
    ) -> bool:
        with self._lock:
            return any(
                (task.workspace_id, task.project_id, task.assignment.actor_id)
                == (workspace_id, project_id, actor_id)
                for task in self._native_tasks.values()
            )

    def native_task(self, workspace_id: UUID, project_id: UUID, task_id: UUID) -> NativeTask | None:
        with self._lock:
            task = self._native_tasks.get(task_id)
            return (
                task
                if task and (task.workspace_id, task.project_id) == (workspace_id, project_id)
                else None
            )

    def native_tasks(
        self, workspace_id: UUID, project_id: UUID, offset: int, limit: int
    ) -> tuple[NativeTask, ...]:
        with self._lock:
            tasks = (
                task
                for task in self._native_tasks.values()
                if (task.workspace_id, task.project_id) == (workspace_id, project_id)
            )
            return tuple(sorted(tasks, key=lambda t: (t.created_at, t.id))[offset : offset + limit])

    def _check_native_task_references(self, task: NativeTask) -> None:
        if self.native_project(task.workspace_id, task.project_id) is None:
            raise InvalidTransitionError("Native project is unavailable")
        if task.assignment.actor_id and not self.native_project_member(
            task.workspace_id, task.project_id, task.assignment.actor_id
        ):
            raise InvalidTransitionError("Native task assignee is not a project member")
        if task.assignment.agent_id and not self.native_agent(
            task.workspace_id, task.project_id, task.assignment.agent_id
        ):
            raise InvalidTransitionError("Native task assignee is not a project agent")

    def insert_native_task(self, task: NativeTask) -> None:
        with self._lock:
            if (
                task.id in self._native_tasks
                or task.version != 1
                or not self._native_creator_exists(
                    task.workspace_id, task.project_id, task.created_by, task.created_by_agent_id
                )
            ):
                raise InvalidTransitionError("Native task already exists or creator is unavailable")
            self._check_native_task_references(task)
            self._native_tasks[task.id] = task

    def update_native_task(self, task: NativeTask, expected_version: int) -> None:
        with self._lock:
            previous = self.native_task(task.workspace_id, task.project_id, task.id)
            if (
                previous is None
                or previous.version != expected_version
                or task.version != expected_version + 1
                or (task.created_by, task.created_by_agent_id, task.created_at)
                != (previous.created_by, previous.created_by_agent_id, previous.created_at)
            ):
                raise InvalidTransitionError("Native task missing or stale task version")
            self._check_native_task_references(task)
            self._native_tasks[task.id] = task

    def _native_creator_exists(
        self,
        workspace_id: UUID,
        project_id: UUID,
        actor_id: UUID | None,
        agent_id: UUID | None,
    ) -> bool:
        if (actor_id is None) == (agent_id is None):
            return False
        if actor_id is not None:
            return (actor_id, workspace_id) in self._memberships
        return (
            agent_id is not None
            and self.native_agent(workspace_id, project_id, agent_id) is not None
        )

    def native_agent(
        self, workspace_id: UUID, project_id: UUID, agent_id: UUID
    ) -> NativeAgent | None:
        with self._lock:
            agent = self._native_agents.get(agent_id)
            return (
                agent
                if agent and (agent.workspace_id, agent.project_id) == (workspace_id, project_id)
                else None
            )

    def native_agents(
        self, workspace_id: UUID, project_id: UUID, offset: int, limit: int
    ) -> tuple[NativeAgent, ...]:
        with self._lock:
            agents = (
                agent
                for agent in self._native_agents.values()
                if (agent.workspace_id, agent.project_id) == (workspace_id, project_id)
            )
            return tuple(
                sorted(agents, key=lambda a: (a.created_at, a.id))[offset : offset + limit]
            )

    def native_active_agent_count(self, workspace_id: UUID, project_id: UUID) -> int:
        with self._lock:
            return sum(
                (agent.workspace_id, agent.project_id, agent.status)
                == (workspace_id, project_id, "active")
                for agent in self._native_agents.values()
            )

    def insert_native_agent(self, agent: NativeAgent) -> None:
        with self._lock:
            if (
                agent.id in self._native_agents
                or agent.version != 1
                or self.native_project(agent.workspace_id, agent.project_id) is None
                or not self._native_creator_exists(
                    agent.workspace_id,
                    agent.project_id,
                    agent.created_by,
                    agent.created_by_agent_id,
                )
                or any(
                    (saved.workspace_id, saved.project_id, saved.role_key)
                    == (agent.workspace_id, agent.project_id, agent.role_key)
                    for saved in self._native_agents.values()
                )
            ):
                raise InvalidTransitionError("Agent role exists or project/creator is unavailable")
            self._native_agents[agent.id] = agent

    def update_native_agent(self, agent: NativeAgent, expected_version: int) -> None:
        with self._lock:
            previous = self.native_agent(agent.workspace_id, agent.project_id, agent.id)
            if (
                previous is None
                or previous.version != expected_version
                or agent.version != expected_version + 1
                or (agent.role_key, agent.created_by, agent.created_by_agent_id, agent.created_at)
                != (
                    previous.role_key,
                    previous.created_by,
                    previous.created_by_agent_id,
                    previous.created_at,
                )
            ):
                raise InvalidTransitionError("Native agent missing or stale agent version")
            self._native_agents[agent.id] = agent

    def native_team_policy(self, workspace_id: UUID, project_id: UUID) -> NativeTeamPolicy | None:
        with self._lock:
            return self._native_team_policies.get((workspace_id, project_id))

    def save_native_team_policy(self, policy: NativeTeamPolicy, expected_version: int) -> None:
        with self._lock:
            previous = self.native_team_policy(policy.workspace_id, policy.project_id)
            if (
                self.native_project(policy.workspace_id, policy.project_id) is None
                or (previous.version if previous else 0) != expected_version
                or policy.version != expected_version + 1
            ):
                raise InvalidTransitionError("Native team policy missing or stale version")
            self._native_team_policies[policy.workspace_id, policy.project_id] = policy

    def native_agent_credential(self, token_id: UUID) -> NativeAgentCredential | None:
        with self._lock:
            return self._native_agent_credentials.get(token_id)

    def native_agent_credentials(
        self, workspace_id: UUID, project_id: UUID, agent_id: UUID
    ) -> tuple[NativeAgentCredential, ...]:
        with self._lock:
            credentials = (
                credential
                for credential in self._native_agent_credentials.values()
                if (credential.workspace_id, credential.project_id, credential.agent_id)
                == (workspace_id, project_id, agent_id)
            )
            return tuple(sorted(credentials, key=lambda c: (c.created_at, c.id)))

    def insert_native_agent_credential(self, credential: NativeAgentCredential) -> None:
        with self._lock:
            agent = self.native_agent(
                credential.workspace_id, credential.project_id, credential.agent_id
            )
            if (
                agent is None
                or agent.version != credential.agent_version
                or agent.status != "active"
                or credential.id in self._native_agent_credentials
                or any(
                    c.token_hash == credential.token_hash
                    for c in self._native_agent_credentials.values()
                )
                or (credential.issued_by, credential.workspace_id) not in self._memberships
                or credential.expires_at <= credential.created_at
                or (
                    credential.revoked_at is not None
                    and credential.revoked_at < credential.created_at
                )
            ):
                raise InvalidTransitionError("Agent credential exists or authority is unavailable")
            self._native_agent_credentials[credential.id] = credential

    def revoke_native_agent_credential(
        self,
        workspace_id: UUID,
        project_id: UUID,
        agent_id: UUID,
        token_id: UUID,
        revoked_at: datetime,
    ) -> bool:
        with self._lock:
            credential = self._native_agent_credentials.get(token_id)
            if (
                credential is None
                or (credential.workspace_id, credential.project_id, credential.agent_id)
                != (workspace_id, project_id, agent_id)
                or credential.revoked_at is not None
            ):
                return False
            if revoked_at < credential.created_at:
                raise InvalidTransitionError("Credential revocation predates issuance")
            self._native_agent_credentials[token_id] = credential.model_copy(
                update={"revoked_at": revoked_at}
            )
            return True

    def native_release_agent_tasks(
        self, workspace_id: UUID, project_id: UUID, agent_id: UUID
    ) -> tuple[NativeTask, ...]:
        with self._lock:
            released = []
            now = utc_now()
            for task in self._native_tasks.values():
                if (task.workspace_id, task.project_id, task.assignment.agent_id) != (
                    workspace_id,
                    project_id,
                    agent_id,
                ) or task.status in {"done", "cancelled"}:
                    continue
                saved = task.model_copy(
                    update={
                        "assignment": TaskAssignment(),
                        "status": "todo" if task.status == "in_progress" else task.status,
                        "version": task.version + 1,
                        "updated_at": now,
                    }
                )
                self._native_tasks[task.id] = saved
                released.append(saved)
            return tuple(sorted(released, key=lambda t: (t.created_at, t.id)))

    def password_for_email(self, email: str) -> PasswordCredential | None:
        with self._lock:
            return next((c for c in self._passwords.values() if c.email == email), None)

    def email_code(self, identifier: UUID) -> EmailCode | None:
        with self._lock:
            return self._email_codes.get(identifier)

    def email_codes(
        self, email: str, purpose: str, since: datetime, *, actor_id: UUID | None = None
    ) -> tuple[EmailCode, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        c
                        for c in self._email_codes.values()
                        if (c.email == email or c.actor_id == actor_id)
                        and c.purpose == purpose
                        and c.created_at >= since
                    ),
                    key=lambda c: c.created_at,
                    reverse=True,
                )
            )

    def save_email_code(self, code: EmailCode) -> None:
        with self._lock:
            self._email_codes[code.id] = code

    def purge_email_codes(self, before: datetime) -> None:
        with self._lock:
            self._email_codes = {
                k: c for k, c in self._email_codes.items() if c.expires_at >= before
            }

    def managed_accounts(self) -> tuple[ManagedAccount, ...]:
        with self._lock:
            return tuple(self._managed_accounts.values())

    def managed_account(self, actor_id: UUID) -> ManagedAccount | None:
        with self._lock:
            return self._managed_accounts.get(actor_id)

    def save_managed_account(self, account: ManagedAccount) -> None:
        with self._lock:
            self._managed_accounts[account.actor_id] = account

    def voice_sessions(self, workspace_id: UUID, actor_id: UUID) -> tuple[VoiceSession, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        s
                        for s in self._voice_sessions.values()
                        if s.workspace_id == workspace_id and s.actor_id == actor_id
                    ),
                    key=lambda s: s.created_at,
                    reverse=True,
                )[:50]
            )

    def voice_session(self, workspace_id: UUID, session_id: UUID) -> VoiceSession | None:
        with self._lock:
            session = self._voice_sessions.get(session_id)
            return session if session and session.workspace_id == workspace_id else None

    def save_voice_session(self, session: VoiceSession) -> None:
        with self._lock:
            self._voice_sessions[session.id] = session

    def response_preferences(
        self, workspace_id: UUID, actor_id: UUID
    ) -> ResponsePreferences | None:
        with self._lock:
            return self._response_preferences.get((workspace_id, actor_id))

    def save_response_preferences(
        self, workspace_id: UUID, actor_id: UUID, preferences: ResponsePreferences
    ) -> None:
        with self._lock:
            self._response_preferences[workspace_id, actor_id] = preferences

    def feedback(self, workspace_id: UUID, actor_id: UUID, run_id: UUID) -> RunFeedback | None:
        with self._lock:
            return self._feedback.get((workspace_id, actor_id, run_id))

    def save_feedback(self, workspace_id: UUID, actor_id: UUID, feedback: RunFeedback) -> None:
        with self._lock:
            self._feedback[workspace_id, actor_id, feedback.run_id] = feedback

    def answer_runs(self, thread_id: UUID, offset: int, limit: int) -> tuple[Run, ...]:
        with self._lock:
            # Message sequence is conversation order, even when clocks tie or move
            # backwards. Legacy snapshots without their output remain before it.
            rows = sorted(
                (r for r in self._runs.values() if r.thread_id == thread_id),
                key=lambda r: (
                    message.sequence
                    if (message := self._messages.get(r.output_message_id)) is not None
                    and message.thread_id == thread_id
                    else 0,
                    r.created_at,
                    r.id,
                ),
            )
            return tuple(rows[offset : offset + limit])

    def latest_run(self, thread_id: UUID) -> Run | None:
        with self._lock:
            return max(
                (r for r in self._runs.values() if r.thread_id == thread_id),
                key=lambda r: (
                    message.sequence
                    if (message := self._messages.get(r.output_message_id)) is not None
                    and message.thread_id == thread_id
                    else 0,
                    r.created_at,
                    r.id,
                ),
                default=None,
            )

    def save_attempt(self, attempt: ModelAttempt) -> None:
        with self._lock:
            self._attempts[attempt.run.id] = attempt

    def attempt(self, run_id: UUID) -> ModelAttempt | None:
        with self._lock:
            return self._attempts.get(run_id)

    def pending_attempt(self, thread_id: UUID) -> ModelAttempt | None:
        with self._lock:
            return next(
                (
                    a
                    for a in self._attempts.values()
                    if a.run.thread_id == thread_id and a.status == "pending"
                ),
                None,
            )

    def recent_messages(self, thread_id: UUID, limit: int) -> tuple[Message, ...]:
        with self._lock:
            rows = sorted(
                (m for m in self._messages.values() if m.thread_id == thread_id),
                key=lambda m: m.sequence,
            )
            return tuple(rows[-limit:])

    def insert_memory(self, memory: ExplicitMemory) -> None:
        with self._lock:
            if memory.id in self._explicit_memories:
                raise InvalidTransitionError("memory already exists")
            self._explicit_memories[memory.id] = memory

    def update_project_memory(
        self, previous: ExplicitMemory, name: str, description: str
    ) -> ExplicitMemory:
        with self._lock:
            current = self._explicit_memories.get(previous.id)
            if current != previous or not previous.accepted or previous.category != "project":
                raise InvalidTransitionError("Project details changed before saving")
            updated = previous.model_copy(update={"subject": name, "content": description})
            self._explicit_memories[previous.id] = updated
            return updated

    def explicit_memories(
        self,
        workspace_id: UUID,
        offset: int,
        limit: int,
        actor_id: UUID | None = None,
        personal: bool = True,
    ) -> tuple[ExplicitMemory, ...]:
        with self._lock:
            rows = sorted(
                (
                    m
                    for m in self._explicit_memories.values()
                    if m.workspace_id == workspace_id
                    and m.accepted
                    and (
                        m.scope == "workspace"
                        or (personal and (actor_id is None or m.created_by == actor_id))
                    )
                ),
                key=lambda m: (m.created_at, m.id),
            )
            return tuple(rows[offset : offset + limit])

    def explicit_memory(self, workspace_id: UUID, memory_id: UUID) -> ExplicitMemory | None:
        with self._lock:
            memory = self._explicit_memories.get(memory_id)
            return memory if memory and memory.workspace_id == workspace_id else None

    def retract_memory(self, memory: ExplicitMemory) -> None:
        with self._lock:
            self._explicit_memories[memory.id] = memory.model_copy(update={"accepted": False})

    def insert_thread(self, thread: Thread) -> None:
        with self._lock:
            if thread.id in self._threads:
                raise InvalidTransitionError("thread already exists")
            self._threads[thread.id] = thread

    def threads(
        self, workspace_id: UUID, offset: int, limit: int, actor_id: UUID | None = None
    ) -> tuple[Thread, ...]:
        with self._lock:
            rows = sorted(
                (
                    t
                    for t in self._threads.values()
                    if t.workspace_id == workspace_id
                    and (
                        actor_id is None or t.visibility == "workspace" or t.created_by == actor_id
                    )
                ),
                key=lambda t: (t.created_at, t.id),
            )
            return tuple(rows[offset : offset + limit])

    def recall_documents(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        terms: tuple[str, ...],
        offset: int,
        limit: int,
        exclude_thread: UUID | None = None,
    ) -> tuple[RecallDocument, ...]:
        with self._lock:
            threads = {
                t.id: t
                for t in self._threads.values()
                if t.workspace_id == workspace_id
                and t.created_by == actor_id
                and t.id != exclude_thread
            }
            voice_threads = {s.thread_id for s in self._voice_sessions.values()}
            documents = [
                RecallDocument(
                    id=m.id,
                    thread_id=m.thread_id,
                    title=threads[m.thread_id].title,
                    source="text",
                    role=m.role,
                    text=m.text,
                    created_at=m.created_at,
                )
                for m in self._messages.values()
                if m.thread_id in threads and m.thread_id not in voice_threads
            ]
            documents.extend(
                RecallDocument(
                    id=s.id,
                    thread_id=s.thread_id,
                    title=threads[s.thread_id].title,
                    source="voice",
                    role="transcript",
                    created_at=s.created_at,
                    text="".join(
                        (
                            f"\n{f.speaker}: "
                            if i == 0 or s.fragments[i - 1].speaker != f.speaker
                            else ""
                        )
                        + f.text
                        for i, f in enumerate(s.fragments)
                    ),
                )
                for s in self._voice_sessions.values()
                if s.thread_id in threads
                and s.actor_id == actor_id
                and s.workspace_id == workspace_id
                and s.fragments
            )

            def score(doc: RecallDocument) -> int:
                text = (doc.title + " " + doc.text).lower()
                return sum(term in text for term in terms)

            documents = [d for d in documents if not terms or score(d)]
            documents.sort(key=lambda d: (score(d), d.created_at, d.id), reverse=True)
            return tuple(documents[offset : offset + limit])

    def thread(self, workspace_id: UUID, thread_id: UUID) -> Thread | None:
        with self._lock:
            row = self._threads.get(thread_id)
            return row if row and row.workspace_id == workspace_id else None

    def messages(self, thread_id: UUID, after: int, limit: int) -> tuple[Message, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        m
                        for m in self._messages.values()
                        if m.thread_id == thread_id and m.sequence > after
                    ),
                    key=lambda m: m.sequence,
                )[:limit]
            )

    def insert_message(self, message: Message) -> None:
        with self._lock:
            if message.id in self._messages or any(
                m.thread_id == message.thread_id and m.sequence == message.sequence
                for m in self._messages.values()
            ):
                raise InvalidTransitionError("message already exists")
            self._messages[message.id] = message

    def insert_run(self, run: Run, events: tuple[RunEvent, ...]) -> None:
        with self._lock:
            if run.id in self._runs:
                raise InvalidTransitionError("run already exists")
            self._runs[run.id] = run
            self._run_events[run.id] = events

    def run(self, run_id: UUID) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def run_events(self, run_id: UUID) -> tuple[RunEvent, ...]:
        with self._lock:
            return self._run_events.get(run_id, ())

    def register(
        self,
        definition: CapabilityDefinition,
        input_model: type[BaseModel],
        handler: CapabilityHandler,
    ) -> None:
        with self._lock:
            if definition.name in self._capabilities:
                raise ValueError(f"capability already registered: {definition.name}")
            self._capabilities[definition.name] = (definition, input_model, handler)

    def get(
        self, name: str
    ) -> tuple[CapabilityDefinition, type[BaseModel], CapabilityHandler] | None:
        with self._lock:
            return self._capabilities.get(name)

    def list(self) -> tuple[CapabilityDefinition, ...]:
        with self._lock:
            registrations = sorted(self._capabilities.values(), key=lambda item: item[0].name)
            return tuple(item[0] for item in registrations)

    def execute_once(
        self,
        namespace: str,
        key: str,
        request_digest: str,
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            lookup = (namespace, key)
            existing = self._invocations.get(lookup)
            if existing:
                if existing[0] != request_digest:
                    raise IdempotencyConflictError("conflicting invocation")
                return deepcopy(existing[1]), True
            result = operation()
            self._invocations[lookup] = (request_digest, deepcopy(result))
            return result, False

    def create_job(self, job: Job) -> tuple[Job, bool]:
        with self._lock:
            lookup = (job.workspace_id, job.kind, job.idempotency_key)
            existing_id = self._job_keys.get(lookup)
            if existing_id:
                existing = self._jobs[existing_id]
                if (
                    existing.input_digest != job.input_digest
                    or existing.created_by != job.created_by
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already used with different job input"
                    )
                return existing.model_copy(deep=True), False
            self._jobs[job.id] = job.model_copy(deep=True)
            self._job_keys[lookup] = job.id
            return job, True

    def get_job(self, job_id: UUID) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    def jobs(
        self, workspace_id: UUID, actor_id: UUID, kind: str, offset: int, limit: int
    ) -> tuple[Job, ...]:
        with self._lock:
            rows = [
                job
                for job in self._jobs.values()
                if job.workspace_id == workspace_id
                and job.created_by == actor_id
                and job.kind == kind
            ]
            rows.sort(
                key=lambda job: (
                    -int(job.input.get("priority", 3)),
                    int(job.input.get("rank", 0)),
                    job.created_at,
                )
            )
            return tuple(job.model_copy(deep=True) for job in rows[offset : offset + limit])

    def project_run_jobs(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        before: tuple[datetime, UUID] | None,
        limit: int,
    ) -> tuple[Job, ...]:
        with self._lock:
            plan_ids = {
                str(job.id)
                for job in self._jobs.values()
                if job.kind == "platform.plan"
                and (job.workspace_id, job.created_by) == (workspace_id, actor_id)
                and job.input.get("plan", {}).get("project_id") == str(project_id)
            }
            rows = [
                job
                for job in self._jobs.values()
                if job.kind == "platform.run"
                and (job.workspace_id, job.created_by) == (workspace_id, actor_id)
                and job.input.get("plan_id") in plan_ids
                and (before is None or (job.created_at, job.id) < before)
            ]
            rows.sort(key=lambda job: (job.created_at, job.id), reverse=True)
            return tuple(job.model_copy(deep=True) for job in rows[:limit])

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
        with self._lock:
            rows = []
            for job in self._jobs.values():
                if (job.workspace_id, job.created_by, job.kind) != (
                    workspace_id,
                    actor_id,
                    "platform.project_activity." + project_id.hex,
                ):
                    continue
                activity = job.input["initial_state"]
                if (
                    (activity_kind is None or activity["kind"] == activity_kind)
                    and (before_sequence is None or activity["sequence"] < before_sequence)
                    and query.lower() in activity["text"].lower()
                ):
                    rows.append(job)
            rows.sort(key=lambda job: int(job.input["initial_state"]["sequence"]), reverse=True)
            return tuple(job.model_copy(deep=True) for job in rows[:limit])

    def jobs_all(self, kind: str, limit: int, status: str = "queued") -> tuple[Job, ...]:
        with self._lock:
            rows = [
                job
                for job in self._jobs.values()
                if job.kind == kind and job.status.value == status
            ]
            rows.sort(
                key=lambda job: (
                    -int(job.input.get("priority", 3)),
                    int(job.input.get("rank", 0)),
                    job.created_at,
                )
            )
            return tuple(job.model_copy(deep=True) for job in rows[:limit])

    def save_job(self, job: Job, expected_version: int) -> Job:
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.version != expected_version:
                raise InvalidTransitionError("job missing or stale job version")
            updated = job.model_copy(update={"version": expected_version + 1})
            self._jobs[job.id] = updated.model_copy(deep=True)
            return updated

    def transition_job(
        self,
        job_id: UUID,
        expected_version: int,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job:
        with self._lock:
            current = self._jobs[job_id]
            if current.version != expected_version:
                raise InvalidTransitionError(
                    f"stale job version: expected {expected_version}, current {current.version}"
                )
            updated = current.model_copy(
                update={
                    "status": status,
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                    "result": result,
                    "error_code": error_code,
                }
            )
            self._jobs[job_id] = updated.model_copy(deep=True)
            return updated

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            events = self.audit_events(event.workspace_id)
            expected_sequence = len(events) + 1
            if event.sequence != expected_sequence:
                raise ValueError("audit sequence is not contiguous")
            expected_previous = events[-1].event_hash if events else "0" * 64
            if event.previous_hash != expected_previous:
                raise ValueError("audit hash chain is invalid")
            self._audit.append(event.model_copy(deep=True))

    def audit_events(self, workspace_id: UUID | None = None) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(
                event.model_copy(deep=True)
                for event in self._audit
                if workspace_id is None or event.workspace_id == workspace_id
            )

    def add_outbox(self, event: OutboxEvent) -> None:
        with self._lock:
            self._outbox.append(event.model_copy(deep=True))

    def outbox_events(self) -> tuple[OutboxEvent, ...]:
        with self._lock:
            return tuple(event.model_copy(deep=True) for event in self._outbox)

    def publish_pending(self, deliver: Callable[[OutboxEvent], None], limit: int = 100) -> int:
        if limit < 1:
            raise ValueError("limit must be positive")
        published = 0
        with self._lock:
            pending = [
                (i, event) for i, event in enumerate(self._outbox) if event.published_at is None
            ][:limit]
            for index, event in pending:
                updated = event.model_copy(update={"attempts": event.attempts + 1})
                try:
                    deliver(updated.model_copy(deep=True))
                except Exception:
                    self._outbox[index] = updated
                else:
                    self._outbox[index] = updated.model_copy(update={"published_at": utc_now()})
                    published += 1
        return published

    def memberships(self, actor_id: UUID) -> tuple[Membership, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (m for m in self._memberships.values() if m.actor_id == actor_id),
                    key=lambda m: str(m.workspace_id),
                )
            )

    def workspace_members(
        self, workspace_id: UUID, offset: int, limit: int
    ) -> tuple[Membership, ...]:
        with self._lock:
            rows = sorted(
                (m for m in self._memberships.values() if m.workspace_id == workspace_id),
                key=lambda m: m.actor_id,
            )
            return tuple(rows[offset : offset + limit])

    def put_membership(self, membership: Membership) -> None:
        with self._lock:
            self._memberships[membership.actor_id, membership.workspace_id] = membership

    def delete_membership(self, actor_id: UUID, workspace_id: UUID) -> None:
        with self._lock:
            revoked_at = utc_now()
            for member in self._native_project_members.values():
                if (member.workspace_id, member.actor_id) == (workspace_id, actor_id):
                    project = self._native_projects[member.project_id]
                    self._native_projects[project.id] = project.model_copy(
                        update={"version": project.version + 1, "updated_at": revoked_at}
                    )
            for task_id, task in self._native_tasks.items():
                if (task.workspace_id, task.assignment.actor_id) == (workspace_id, actor_id):
                    self._native_tasks[task_id] = task.model_copy(
                        update={
                            "assignment": TaskAssignment(),
                            "status": "todo" if task.status == "in_progress" else task.status,
                            "version": task.version + 1,
                            "updated_at": revoked_at,
                        }
                    )
            self._native_project_members = {
                key: member
                for key, member in self._native_project_members.items()
                if (member.workspace_id, member.actor_id) != (workspace_id, actor_id)
            }
            self._memberships.pop((actor_id, workspace_id), None)

    def save_session(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.token_hash] = session

    def get_session(self, token_hash: str) -> Session | None:
        with self._lock:
            return self._sessions.get(token_hash)

    def delete_session(self, token_hash: str) -> None:
        with self._lock:
            self._sessions.pop(token_hash, None)

    def revoke_sessions(self, actor_id: UUID) -> None:
        with self._lock:
            self._sessions = {
                key: value for key, value in self._sessions.items() if value.actor_id != actor_id
            }

    def save_enrollment(self, enrollment: Enrollment) -> None:
        with self._lock:
            self._enrollments[enrollment.token_hash] = enrollment

    def get_enrollment(self, token_hash: str) -> Enrollment | None:
        with self._lock:
            return self._enrollments.get(token_hash)

    def delete_enrollment(self, token_hash: str) -> None:
        with self._lock:
            self._enrollments.pop(token_hash, None)

    def save_challenge(self, challenge: Challenge) -> None:
        with self._lock:
            self._challenges = {
                key: value
                for key, value in self._challenges.items()
                if value.expires_at > utc_now()
            }
            self._challenges[challenge.token_hash] = challenge

    def take_challenge(self, token_hash: str, binding_hash: str) -> Challenge | None:
        with self._lock:
            challenge = self._challenges.get(token_hash)
            if challenge is None or challenge.binding_hash != binding_hash:
                return None
            return self._challenges.pop(token_hash)

    def passkeys(self, actor_id: UUID) -> tuple[Passkey, ...]:
        with self._lock:
            return tuple(key for key in self._passkeys.values() if key.actor_id == actor_id)

    def get_passkey(self, credential_id: str) -> Passkey | None:
        with self._lock:
            return self._passkeys.get(credential_id)

    def save_passkey(self, credential: Passkey) -> None:
        with self._lock:
            if credential.credential_id in self._passkeys:
                raise AuthenticationError("passkey is already registered")
            self._passkeys[credential.credential_id] = credential

    def update_passkey(self, credential: Passkey) -> None:
        with self._lock:
            self._passkeys[credential.credential_id] = credential

    def delete_passkey(self, credential_id: str) -> None:
        with self._lock:
            self._passkeys.pop(credential_id, None)

    def password_for_actor(self, actor_id: UUID) -> PasswordCredential | None:
        with self._lock:
            return self._passwords.get(actor_id)

    def password_for_username(self, username: str) -> PasswordCredential | None:
        with self._lock:
            return next(
                (item for item in self._passwords.values() if item.username == username), None
            )

    def save_password(self, credential: PasswordCredential) -> None:
        with self._lock:
            if any(
                item.actor_id != credential.actor_id and item.username == credential.username
                for item in self._passwords.values()
            ):
                raise AuthenticationError("username is already in use")
            if credential.email and any(
                item.actor_id != credential.actor_id and item.email == credential.email
                for item in self._passwords.values()
            ):
                raise AuthenticationError("email is already in use")
            self._passwords[credential.actor_id] = credential

    def integration_connections(
        self, workspace_id: UUID, actor_id: UUID
    ) -> tuple[IntegrationConnection, ...]:
        with self._lock:
            return tuple(
                value.model_copy(deep=True)
                for key, value in self._integrations.items()
                if key[:2] == (workspace_id, actor_id)
            )

    def save_integration_connection(self, connection: IntegrationConnection) -> None:
        with self._lock:
            self._integrations[(connection.workspace_id, connection.actor_id, connection.id)] = (
                connection.model_copy(deep=True)
            )

    def delete_integration_connection(
        self, workspace_id: UUID, actor_id: UUID, identifier: str
    ) -> None:
        with self._lock:
            self._integrations.pop((workspace_id, actor_id, identifier), None)

    def google_connections(
        self, workspace_id: UUID, actor_id: UUID
    ) -> tuple[GoogleConnection, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        c
                        for c in self._google.values()
                        if (c.workspace_id, c.actor_id) == (workspace_id, actor_id)
                    ),
                    key=lambda c: (not c.is_default, c.email.casefold()),
                )
            )

    def google_connection(self, workspace_id: UUID, actor_id: UUID) -> GoogleConnection | None:
        connections = self.google_connections(workspace_id, actor_id)
        return connections[0] if connections else None

    def save_google_connection(self, connection: GoogleConnection) -> None:
        with self._lock:
            key = (connection.workspace_id, connection.actor_id, connection.email.casefold())
            old = self._google.get(key)
            default = old.is_default if old else not self.google_connections(*key[:2])
            self._google[key] = connection.model_copy(update={"is_default": default})

    def set_default_google_connection(
        self, workspace_id: UUID, actor_id: UUID, connection_id: UUID
    ) -> None:
        with self._lock:
            connections = self.google_connections(workspace_id, actor_id)
            if not any(c.id == connection_id for c in connections):
                raise NotFoundError("Google account not found.")
            for c in connections:
                self._google[workspace_id, actor_id, c.email.casefold()] = c.model_copy(
                    update={"is_default": c.id == connection_id}
                )

    def delete_google_connection(
        self, workspace_id: UUID, actor_id: UUID, connection_id: UUID | None = None
    ) -> None:
        with self._lock:
            for c in self.google_connections(workspace_id, actor_id):
                if connection_id is None or c.id == connection_id:
                    self._google.pop((workspace_id, actor_id, c.email.casefold()))
            remaining = self.google_connections(workspace_id, actor_id)
            if remaining and not any(c.is_default for c in remaining):
                self.set_default_google_connection(workspace_id, actor_id, remaining[0].id)

    def save_google_state(self, state: GoogleOAuthState) -> None:
        with self._lock:
            self._google_states = {
                k: v for k, v in self._google_states.items() if v.expires_at > utc_now()
            }
            self._google_states[state.state_hash] = state

    def take_google_state(self, state_hash: str, binding_hash: str) -> GoogleOAuthState | None:
        with self._lock:
            state = self._google_states.get(state_hash)
            if not state or state.binding_hash != binding_hash:
                return None
            return self._google_states.pop(state_hash)

    def action(self, action_id: UUID) -> ActionProposal | None:
        with self._lock:
            return self._actions.get(action_id)

    def recent_actions(
        self, thread_id: UUID, actor_id: UUID, limit: int
    ) -> tuple[ActionProposal, ...]:
        with self._lock:
            action_ids = {
                action_id
                for run in self._runs.values()
                if run.thread_id == thread_id
                for action_id in run.action_ids
            }
            action_ids.update(
                action.id
                for action in self._actions.values()
                if action.immediate
                and (attempt := self._attempts.get(action.run_id))
                and attempt.run.thread_id == thread_id
                and attempt.workspace_id == action.workspace_id
            )
            return tuple(
                sorted(
                    (
                        action
                        for action in self._actions.values()
                        if action.id in action_ids and action.actor_id == actor_id
                    ),
                    key=lambda action: (action.created_at, action.id),
                    reverse=True,
                )[:limit]
            )

    def save_action(self, action: ActionProposal) -> None:
        with self._lock:
            self._actions[action.id] = action
