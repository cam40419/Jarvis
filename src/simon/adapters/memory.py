from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from simon.domain.connected_tools import ActionProposal, GoogleConnection, GoogleOAuthState
from simon.domain.context import ExplicitMemory
from simon.domain.conversations import Message, ModelAttempt, Run, RunEvent, Thread
from simon.domain.errors import (
    AuthenticationError,
    IdempotencyConflictError,
    InvalidTransitionError,
)
from simon.domain.home import HomeCommand, HomeDevice, HomeSync, PowerSample
from simon.domain.identity import Challenge, Enrollment, Membership, Passkey, Session
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
from simon.domain.voice import VoiceSession


class InMemoryStore:
    """Thread-safe reference adapter used for tests and local smoke runs."""

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
        self._threads: dict[UUID, Thread] = {}
        self._messages: dict[UUID, Message] = {}
        self._runs: dict[UUID, Run] = {}
        self._run_events: dict[UUID, tuple[RunEvent, ...]] = {}
        self._explicit_memories: dict[UUID, ExplicitMemory] = {}
        self._attempts: dict[UUID, ModelAttempt] = {}
        self._response_preferences: dict[tuple[UUID, UUID], ResponsePreferences] = {}
        self._google: dict[tuple[UUID, UUID], GoogleConnection] = {}
        self._google_states: dict[str, GoogleOAuthState] = {}
        self._actions: dict[UUID, ActionProposal] = {}
        self._home_devices: dict[tuple[UUID, str], HomeDevice] = {}
        self._home_syncs: dict[tuple[UUID, str], HomeSync] = {}
        self._home_commands: dict[UUID, HomeCommand] = {}
        self._power_samples: dict[tuple[UUID, str, datetime], PowerSample] = {}
        self._feedback: dict[tuple[UUID, UUID, UUID], RunFeedback] = {}
        self._voice_sessions: dict[UUID, VoiceSession] = {}

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
                    self._home_devices,
                    self._home_syncs,
                    self._home_commands,
                    self._power_samples,
                    self._voice_sessions,
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
                    self._home_devices,
                    self._home_syncs,
                    self._home_commands,
                    self._power_samples,
                    self._voice_sessions,
                ) = snapshot
                raise

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

    def home_command(
        self, household_id: UUID, actor_id: UUID, command_id: UUID
    ) -> HomeCommand | None:
        with self._lock:
            command = self._home_commands.get(command_id)
            return (
                command
                if command and (command.household_id, command.actor_id) == (household_id, actor_id)
                else None
            )

    def power_samples(
        self, household_id: UUID, device_id: str, start: datetime, end: datetime, limit: int = 3000
    ) -> tuple[PowerSample, ...]:
        with self._lock:
            rows = [
                s
                for (h, d, _), s in self._power_samples.items()
                if h == household_id and d == device_id and start <= s.captured_at < end
            ]
            return tuple(sorted(rows, key=lambda s: s.captured_at, reverse=True)[:limit])

    def save_power_sample(self, sample: PowerSample) -> None:
        with self._lock:
            self._power_samples[sample.household_id, sample.device_id, sample.captured_at] = sample

    def prune_power_samples(self, household_id: UUID, before: datetime) -> None:
        with self._lock:
            self._power_samples = {
                k: s
                for k, s in self._power_samples.items()
                if s.household_id != household_id or s.captured_at >= before
            }

    def home_commands(
        self, household_id: UUID, actor_id: UUID, run_id: UUID | None = None
    ) -> tuple[HomeCommand, ...]:
        with self._lock:
            rows = sorted(
                (
                    c
                    for c in self._home_commands.values()
                    if c.household_id == household_id
                    and c.actor_id == actor_id
                    and (run_id is None or c.run_id == run_id)
                ),
                key=lambda c: (c.created_at, c.id),
                reverse=True,
            )
            return tuple(rows[:100])

    def save_home_command(self, command: HomeCommand) -> None:
        with self._lock:
            self._home_commands[command.id] = command

    def home_devices(self, household_id: UUID) -> tuple[HomeDevice, ...]:
        with self._lock:
            return tuple(d for (h, _), d in self._home_devices.items() if h == household_id)

    def save_home_device(self, device: HomeDevice) -> None:
        with self._lock:
            self._home_devices[device.household_id, device.id] = device

    def home_sync(self, household_id: UUID, provider: str) -> HomeSync | None:
        with self._lock:
            return self._home_syncs.get((household_id, provider))

    def save_home_sync(self, sync: HomeSync) -> None:
        with self._lock:
            self._home_syncs[sync.household_id, sync.provider] = sync

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
            rows = sorted(
                (r for r in self._runs.values() if r.thread_id == thread_id),
                key=lambda r: (r.created_at, r.id),
            )
            return tuple(rows[offset : offset + limit])

    def latest_run(self, thread_id: UUID) -> Run | None:
        with self._lock:
            return max(
                (r for r in self._runs.values() if r.thread_id == thread_id),
                key=lambda r: (r.created_at, r.id),
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
        self, household_id: UUID, offset: int, limit: int
    ) -> tuple[ExplicitMemory, ...]:
        with self._lock:
            rows = sorted(
                (
                    m
                    for m in self._explicit_memories.values()
                    if m.household_id == household_id and m.accepted
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

    def threads(self, household_id: UUID, offset: int, limit: int) -> tuple[Thread, ...]:
        with self._lock:
            rows = sorted(
                (t for t in self._threads.values() if t.household_id == household_id),
                key=lambda t: (t.created_at, t.id),
            )
            return tuple(rows[offset : offset + limit])

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

    def google_connection(self, household_id: UUID, actor_id: UUID) -> GoogleConnection | None:
        with self._lock:
            return self._google.get((household_id, actor_id))

    def save_google_connection(self, connection: GoogleConnection) -> None:
        with self._lock:
            self._google[connection.household_id, connection.actor_id] = connection

    def delete_google_connection(self, household_id: UUID, actor_id: UUID) -> None:
        with self._lock:
            self._google.pop((household_id, actor_id), None)

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

    def save_action(self, action: ActionProposal) -> None:
        with self._lock:
            self._actions[action.id] = action
