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
from simon.domain.interaction import ResponsePreferences, RunFeedback
from simon.domain.models import (
    AuditEvent,
    CapabilityDefinition,
    Job,
    JobStatus,
    OutboxEvent,
    utc_now,
)
from simon.domain.ports import CapabilityHandler
from simon.domain.project_files import ProjectDrive, ProjectFileOperation
from simon.domain.tasks import ProjectArtifact
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
        self._threads: dict[UUID, Thread] = {}
        self._messages: dict[UUID, Message] = {}
        self._runs: dict[UUID, Run] = {}
        self._run_events: dict[UUID, tuple[RunEvent, ...]] = {}
        self._explicit_memories: dict[UUID, ExplicitMemory] = {}
        self._attempts: dict[UUID, ModelAttempt] = {}
        self._response_preferences: dict[tuple[UUID, UUID], ResponsePreferences] = {}
        self._google: dict[tuple[UUID, UUID, str], GoogleConnection] = {}
        self._google_states: dict[str, GoogleOAuthState] = {}
        self._actions: dict[UUID, ActionProposal] = {}
        self._feedback: dict[tuple[UUID, UUID, UUID], RunFeedback] = {}
        self._voice_sessions: dict[UUID, VoiceSession] = {}
        self._managed_accounts: dict[UUID, ManagedAccount] = {}
        self._project_artifacts: dict[UUID, tuple[ProjectArtifact, bytes]] = {}
        self._project_drive: dict[tuple[UUID, UUID, UUID], ProjectDrive] = {}
        self._project_file_ops: dict[UUID, ProjectFileOperation] = {}

    @contextmanager
    def transaction(self, household_id: UUID | None = None) -> Iterator[None]:
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
                    self._threads,
                    self._messages,
                    self._runs,
                    self._run_events,
                    self._explicit_memories,
                    self._attempts,
                    self._response_preferences,
                    self._feedback,
                    self._google,
                    self._google_states,
                    self._actions,
                    self._voice_sessions,
                    self._managed_accounts,
                    self._project_artifacts,
                    self._project_drive,
                    self._project_file_ops,
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
                    self._threads,
                    self._messages,
                    self._runs,
                    self._run_events,
                    self._explicit_memories,
                    self._attempts,
                    self._response_preferences,
                    self._feedback,
                    self._google,
                    self._google_states,
                    self._actions,
                    self._voice_sessions,
                    self._managed_accounts,
                    self._project_artifacts,
                    self._project_drive,
                    self._project_file_ops,
                ) = snapshot
                raise



























    def save_project_artifact(self, artifact: ProjectArtifact, content: bytes) -> None:
        with self._lock:
            existing = self._project_artifacts.get(artifact.id)
            if existing and existing != (artifact, content):
                raise InvalidTransitionError("project artifact already exists")
            self._project_artifacts[artifact.id] = (artifact, content)

    def project_artifact(self, identifier: UUID) -> tuple[ProjectArtifact, bytes] | None:
        with self._lock:
            return self._project_artifacts.get(identifier)

    def project_artifacts(
        self,
        household_id: UUID,
        actor_id: UUID,
        project_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[ProjectArtifact, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        artifact
                        for artifact, _content in self._project_artifacts.values()
                        if artifact.household_id == household_id
                        and artifact.actor_id == actor_id
                        and (project_id is None or artifact.project_id == project_id)
                    ),
                    key=lambda artifact: (artifact.created_at, artifact.id),
                    reverse=True,
                )[offset : offset + limit]
            )

    def managed_accounts(self) -> tuple[ManagedAccount, ...]:
        with self._lock:
            return tuple(self._managed_accounts.values())

    def managed_account(self, actor_id: UUID) -> ManagedAccount | None:
        with self._lock:
            return self._managed_accounts.get(actor_id)

    def save_managed_account(self, account: ManagedAccount) -> None:
        with self._lock:
            self._managed_accounts[account.actor_id] = account

    def voice_sessions(self, household_id: UUID, actor_id: UUID) -> tuple[VoiceSession, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        s
                        for s in self._voice_sessions.values()
                        if s.household_id == household_id and s.actor_id == actor_id
                    ),
                    key=lambda s: s.created_at,
                    reverse=True,
                )[:50]
            )

    def voice_session(self, household_id: UUID, session_id: UUID) -> VoiceSession | None:
        with self._lock:
            session = self._voice_sessions.get(session_id)
            return session if session and session.household_id == household_id else None

    def save_voice_session(self, session: VoiceSession) -> None:
        with self._lock:
            self._voice_sessions[session.id] = session











    def response_preferences(
        self, household_id: UUID, actor_id: UUID
    ) -> ResponsePreferences | None:
        with self._lock:
            return self._response_preferences.get((household_id, actor_id))

    def save_response_preferences(
        self, household_id: UUID, actor_id: UUID, preferences: ResponsePreferences
    ) -> None:
        with self._lock:
            self._response_preferences[household_id, actor_id] = preferences

    def feedback(self, household_id: UUID, actor_id: UUID, run_id: UUID) -> RunFeedback | None:
        with self._lock:
            return self._feedback.get((household_id, actor_id, run_id))

    def save_feedback(self, household_id: UUID, actor_id: UUID, feedback: RunFeedback) -> None:
        with self._lock:
            self._feedback[household_id, actor_id, feedback.run_id] = feedback

    def answer_runs(self, thread_id: UUID, offset: int, limit: int) -> tuple[Run, ...]:
        with self._lock:
            # Message sequence is conversation order, even when clocks tie or move
            # backwards. Legacy snapshots without their output remain before it.
            rows = sorted(
                (r for r in self._runs.values() if r.thread_id == thread_id),
                key=lambda r: (
                    message.sequence
                    if (message := self._messages.get(r.output_message_id)) is not None
                    and message.thread_id == thread_id else 0,
                    r.created_at, r.id,
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
                    and message.thread_id == thread_id else 0,
                    r.created_at, r.id,
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

    def explicit_memories(
        self,
        household_id: UUID,
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
                    if m.household_id == household_id
                    and m.accepted
                    and (
                        m.scope == "household"
                        or (personal and (actor_id is None or m.created_by == actor_id))
                    )
                ),
                key=lambda m: (m.created_at, m.id),
            )
            return tuple(rows[offset : offset + limit])

    def explicit_memory(self, household_id: UUID, memory_id: UUID) -> ExplicitMemory | None:
        with self._lock:
            memory = self._explicit_memories.get(memory_id)
            return memory if memory and memory.household_id == household_id else None

    def retract_memory(self, memory: ExplicitMemory) -> None:
        with self._lock:
            self._explicit_memories[memory.id] = memory.model_copy(update={"accepted": False})

    def insert_thread(self, thread: Thread) -> None:
        with self._lock:
            if thread.id in self._threads:
                raise InvalidTransitionError("thread already exists")
            self._threads[thread.id] = thread

    def threads(
        self, household_id: UUID, offset: int, limit: int, actor_id: UUID | None = None
    ) -> tuple[Thread, ...]:
        with self._lock:
            rows = sorted(
                (
                    t
                    for t in self._threads.values()
                    if t.household_id == household_id
                    and (
                        actor_id is None or t.visibility == "household" or t.created_by == actor_id
                    )
                ),
                key=lambda t: (t.created_at, t.id),
            )
            return tuple(rows[offset : offset + limit])

    def recall_documents(
        self,
        household_id: UUID,
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
                if t.household_id == household_id
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
                and s.household_id == household_id
                and s.fragments
            )

            def score(doc: RecallDocument) -> int:
                text = (doc.title + " " + doc.text).lower()
                return sum(term in text for term in terms)

            documents = [d for d in documents if not terms or score(d)]
            documents.sort(key=lambda d: (score(d), d.created_at, d.id), reverse=True)
            return tuple(documents[offset : offset + limit])

    def thread(self, household_id: UUID, thread_id: UUID) -> Thread | None:
        with self._lock:
            row = self._threads.get(thread_id)
            return row if row and row.household_id == household_id else None

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
            lookup = (job.household_id, job.kind, job.idempotency_key)
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
        self, household_id: UUID, actor_id: UUID, kind: str, offset: int, limit: int
    ) -> tuple[Job, ...]:
        with self._lock:
            rows = [
                job
                for job in self._jobs.values()
                if job.household_id == household_id
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
        self, household_id: UUID, actor_id: UUID, project_id: UUID,
        before: tuple[datetime, UUID] | None, limit: int,
    ) -> tuple[Job, ...]:
        with self._lock:
            plan_ids = {
                str(job.id) for job in self._jobs.values()
                if job.kind == "platform.plan"
                and (job.household_id, job.created_by) == (household_id, actor_id)
                and job.input.get("plan", {}).get("project_id") == str(project_id)
            }
            rows = [
                job for job in self._jobs.values()
                if job.kind == "platform.run"
                and (job.household_id, job.created_by) == (household_id, actor_id)
                and job.input.get("plan_id") in plan_ids
                and (before is None or (job.created_at, job.id) < before)
            ]
            rows.sort(key=lambda job: (job.created_at, job.id), reverse=True)
            return tuple(job.model_copy(deep=True) for job in rows[:limit])

    def project_activity_jobs(
        self, household_id: UUID, actor_id: UUID, project_id: UUID,
        query: str, activity_kind: str | None, before_sequence: int | None, limit: int,
    ) -> tuple[Job, ...]:
        with self._lock:
            rows = []
            for job in self._jobs.values():
                if (job.household_id, job.created_by, job.kind) != (
                    household_id, actor_id, "platform.project_activity." + project_id.hex,
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
            events = self.audit_events(event.household_id)
            expected_sequence = len(events) + 1
            if event.sequence != expected_sequence:
                raise ValueError("audit sequence is not contiguous")
            expected_previous = events[-1].event_hash if events else "0" * 64
            if event.previous_hash != expected_previous:
                raise ValueError("audit hash chain is invalid")
            self._audit.append(event.model_copy(deep=True))

    def audit_events(self, household_id: UUID | None = None) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(
                event.model_copy(deep=True)
                for event in self._audit
                if household_id is None or event.household_id == household_id
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
                    key=lambda m: str(m.household_id),
                )
            )

    def put_membership(self, membership: Membership) -> None:
        with self._lock:
            self._memberships[membership.actor_id, membership.household_id] = membership

    def delete_membership(self, actor_id: UUID, household_id: UUID) -> None:
        with self._lock:
            self._memberships.pop((actor_id, household_id), None)

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
            self._passwords[credential.actor_id] = credential

    def google_connections(
        self, household_id: UUID, actor_id: UUID
    ) -> tuple[GoogleConnection, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        c
                        for c in self._google.values()
                        if (c.household_id, c.actor_id) == (household_id, actor_id)
                    ),
                    key=lambda c: (not c.is_default, c.email.casefold()),
                )
            )

    def google_connection(self, household_id: UUID, actor_id: UUID) -> GoogleConnection | None:
        connections = self.google_connections(household_id, actor_id)
        return connections[0] if connections else None

    def save_google_connection(self, connection: GoogleConnection) -> None:
        with self._lock:
            key = (connection.household_id, connection.actor_id, connection.email.casefold())
            old = self._google.get(key)
            default = old.is_default if old else not self.google_connections(*key[:2])
            self._google[key] = connection.model_copy(update={"is_default": default})

    def set_default_google_connection(
        self, household_id: UUID, actor_id: UUID, connection_id: UUID
    ) -> None:
        with self._lock:
            connections = self.google_connections(household_id, actor_id)
            if not any(c.id == connection_id for c in connections):
                raise NotFoundError("Google account not found.")
            for c in connections:
                self._google[household_id, actor_id, c.email.casefold()] = c.model_copy(
                    update={"is_default": c.id == connection_id}
                )

    def delete_google_connection(
        self, household_id: UUID, actor_id: UUID, connection_id: UUID | None = None
    ) -> None:
        with self._lock:
            for c in self.google_connections(household_id, actor_id):
                if connection_id is None or c.id == connection_id:
                    self._google.pop((household_id, actor_id, c.email.casefold()))
            remaining = self.google_connections(household_id, actor_id)
            if remaining and not any(c.is_default for c in remaining):
                self.set_default_google_connection(household_id, actor_id, remaining[0].id)

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

    def google_accounts(self) -> tuple[tuple[UUID, UUID], ...]:
        with self._lock:
            return tuple(dict.fromkeys((h, a) for h, a, _ in self._google))

    def project_drive(
        self,
        household_id: UUID,
        actor_id: UUID,
        project_id: UUID,
    ) -> ProjectDrive | None:
        with self._lock:
            return self._project_drive.get((household_id, actor_id, project_id))

    def save_project_drive(self, binding: ProjectDrive) -> None:
        with self._lock:
            key = (binding.household_id, binding.actor_id, binding.project_id)
            self._project_drive[key] = binding

    def project_file_operation(self, identifier: UUID) -> ProjectFileOperation | None:
        with self._lock:
            return self._project_file_ops.get(identifier)

    def save_project_file_operation(self, operation: ProjectFileOperation) -> None:
        with self._lock:
            self._project_file_ops[operation.id] = operation

    def project_file_operations(
        self,
        household_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        limit: int,
    ) -> tuple[ProjectFileOperation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        operation
                        for operation in self._project_file_ops.values()
                        if (operation.household_id, operation.actor_id, operation.project_id)
                        == (household_id, actor_id, project_id)
                    ),
                    key=lambda operation: operation.created_at,
                    reverse=True,
                )[:limit]
            )

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
                and attempt.household_id == action.household_id
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
