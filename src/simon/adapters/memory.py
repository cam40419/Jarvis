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
from simon.domain.workflows import (
    WorkerHeartbeat,
    WorkflowDefinition,
    WorkflowEvent,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTrigger,
)


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
        self._home_devices: dict[tuple[UUID, str], HomeDevice] = {}
        self._home_syncs: dict[tuple[UUID, str], HomeSync] = {}
        self._home_commands: dict[UUID, HomeCommand] = {}
        self._power_samples: dict[tuple[UUID, str, datetime], PowerSample] = {}
        self._feedback: dict[tuple[UUID, UUID, UUID], RunFeedback] = {}
        self._voice_sessions: dict[UUID, VoiceSession] = {}
        self._managed_accounts: dict[UUID, ManagedAccount] = {}
        self._workflow_versions: dict[tuple[UUID, int], WorkflowDefinition] = {}
        self._deleted_workflows: set[UUID] = set()
        self._workflow_runs: dict[UUID, WorkflowRun] = {}
        self._workflow_events: dict[tuple[UUID, int], WorkflowEvent] = {}
        self._workflow_workers: dict[str, WorkerHeartbeat] = {}
        self._workflow_schedules: dict[UUID, WorkflowSchedule] = {}
        self._workflow_triggers: dict[UUID, WorkflowTrigger] = {}
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
                    self._home_devices,
                    self._home_syncs,
                    self._home_commands,
                    self._power_samples,
                    self._voice_sessions,
                    self._managed_accounts,
                    self._workflow_versions,
                    self._deleted_workflows,
                    self._workflow_runs,
                    self._workflow_events,
                    self._workflow_workers,
                    self._workflow_schedules,
                    self._workflow_triggers,
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
                    self._home_devices,
                    self._home_syncs,
                    self._home_commands,
                    self._power_samples,
                    self._voice_sessions,
                    self._managed_accounts,
                    self._workflow_versions,
                    self._deleted_workflows,
                    self._workflow_runs,
                    self._workflow_events,
                    self._workflow_workers,
                    self._workflow_schedules,
                    self._workflow_triggers,
                    self._project_artifacts,
                    self._project_drive,
                    self._project_file_ops,
                ) = snapshot
                raise

    def workflow(self, identifier: UUID, version: int | None = None) -> WorkflowDefinition | None:
        with self._lock:
            return max(
                (
                    w
                    for (i, v), w in self._workflow_versions.items()
                    if i == identifier
                    and i not in self._deleted_workflows
                    and (version is None or v == version)
                ),
                key=lambda w: w.version,
                default=None,
            )

    def workflows(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowDefinition, ...]:
        with self._lock:
            latest: dict[UUID, WorkflowDefinition] = {}
            for w in self._workflow_versions.values():
                if (
                    w.household_id == household_id
                    and w.actor_id == actor_id
                    and w.id not in self._deleted_workflows
                    and (w.id not in latest or latest[w.id].version < w.version)
                ):
                    latest[w.id] = w
            return tuple(
                sorted(latest.values(), key=lambda w: (w.created_at, w.id), reverse=True)[
                    offset : offset + limit
                ]
            )

    def insert_workflow(self, definition: WorkflowDefinition) -> None:
        with self._lock:
            key = definition.id, definition.version
            if key in self._workflow_versions:
                raise InvalidTransitionError("workflow version already exists")
            self._workflow_versions[key] = definition

    def delete_workflow(self, identifier: UUID, household_id: UUID, actor_id: UUID) -> bool:
        with self._lock:
            owned = any(
                workflow.id == identifier
                and workflow.household_id == household_id
                and workflow.actor_id == actor_id
                for workflow in self._workflow_versions.values()
            )
            if owned and identifier not in self._deleted_workflows:
                self._deleted_workflows.add(identifier)
                return True
            return False

    def workflow_run(self, identifier: UUID) -> WorkflowRun | None:
        with self._lock:
            return self._workflow_runs.get(identifier)

    def workflow_runs(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowRun, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        r
                        for r in self._workflow_runs.values()
                        if r.household_id == household_id and r.actor_id == actor_id
                    ),
                    key=lambda r: (r.created_at, r.id),
                    reverse=True,
                )[offset : offset + limit]
            )

    def save_workflow_run(self, run: WorkflowRun, expected_version: int) -> None:
        with self._lock:
            old = self._workflow_runs.get(run.id)
            if (old.version if old else 0) != expected_version:
                raise InvalidTransitionError("workflow run changed; reload before retrying")
            self._workflow_runs[run.id] = run

    def due_workflows(self, now: datetime, limit: int) -> tuple[WorkflowRun, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        r
                        for r in self._workflow_runs.values()
                        if r.next_wake_at is not None and r.next_wake_at <= now
                    ),
                    key=lambda r: (r.next_wake_at, str(r.id)),
                )[:limit]
            )

    def append_workflow_event(self, event: WorkflowEvent) -> None:
        with self._lock:
            key = event.run_id, event.sequence
            if key in self._workflow_events:
                raise InvalidTransitionError("workflow event already exists")
            self._workflow_events[key] = event

    def workflow_events(self, run_id: UUID, after: int) -> tuple[WorkflowEvent, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        e
                        for e in self._workflow_events.values()
                        if e.run_id == run_id and e.sequence > after
                    ),
                    key=lambda e: e.sequence,
                )[:100]
            )

    def worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        with self._lock:
            self._workflow_workers[heartbeat.id] = heartbeat

    def workflow_workers(self) -> tuple[WorkerHeartbeat, ...]:
        with self._lock:
            return tuple(
                sorted(self._workflow_workers.values(), key=lambda w: w.seen_at, reverse=True)[:20]
            )

    def workflow_schedule(self, identifier: UUID) -> WorkflowSchedule | None:
        with self._lock:
            return self._workflow_schedules.get(identifier)

    def workflow_schedules(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowSchedule, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        schedule
                        for schedule in self._workflow_schedules.values()
                        if schedule.household_id == household_id and schedule.actor_id == actor_id
                    ),
                    key=lambda schedule: (schedule.created_at, schedule.id),
                    reverse=True,
                )[offset : offset + limit]
            )

    def due_workflow_schedules(self, now: datetime, limit: int) -> tuple[WorkflowSchedule, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        schedule
                        for schedule in self._workflow_schedules.values()
                        if schedule.enabled
                        and schedule.next_run_at is not None
                        and schedule.next_run_at <= now
                    ),
                    key=lambda schedule: (schedule.next_run_at, schedule.id),
                )[:limit]
            )

    def save_workflow_schedule(self, schedule: WorkflowSchedule, expected_version: int) -> None:
        with self._lock:
            old = self._workflow_schedules.get(schedule.id)
            if (old.version if old else 0) != expected_version:
                raise InvalidTransitionError("workflow schedule changed; reload before retrying")
            self._workflow_schedules[schedule.id] = schedule

    def delete_workflow_schedule(
        self, identifier: UUID, household_id: UUID, actor_id: UUID
    ) -> bool:
        with self._lock:
            schedule = self._workflow_schedules.get(identifier)
            if not schedule or (schedule.household_id, schedule.actor_id) != (
                household_id,
                actor_id,
            ):
                return False
            del self._workflow_schedules[identifier]
            return True

    def delete_workflow_schedules(
        self, definition_id: UUID, household_id: UUID, actor_id: UUID
    ) -> int:
        with self._lock:
            identifiers = [
                schedule.id
                for schedule in self._workflow_schedules.values()
                if schedule.definition_id == definition_id
                and schedule.household_id == household_id
                and schedule.actor_id == actor_id
            ]
            for identifier in identifiers:
                del self._workflow_schedules[identifier]
            return len(identifiers)

    def workflow_trigger(self, identifier: UUID) -> WorkflowTrigger | None:
        with self._lock:
            return self._workflow_triggers.get(identifier)

    def workflow_trigger_for_definition(self, definition_id: UUID) -> WorkflowTrigger | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self._workflow_triggers.values()
                    if item.definition_id == definition_id
                ),
                None,
            )

    def workflow_triggers_for_definition(self, definition_id: UUID) -> tuple[WorkflowTrigger, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._workflow_triggers.values()
                if item.definition_id == definition_id
            )

    def workflow_triggers(
        self, household_id: UUID, actor_id: UUID, offset: int, limit: int
    ) -> tuple[WorkflowTrigger, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._workflow_triggers.values()
                        if item.household_id == household_id and item.actor_id == actor_id
                    ),
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )[offset : offset + limit]
            )

    def due_workflow_triggers(self, now: datetime, limit: int) -> tuple[WorkflowTrigger, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._workflow_triggers.values()
                        if item.enabled
                        and item.next_check_at is not None
                        and item.next_check_at <= now
                    ),
                    key=lambda item: (item.next_check_at, item.id),
                )[:limit]
            )

    def save_workflow_trigger(self, trigger: WorkflowTrigger, expected_version: int) -> None:
        with self._lock:
            old = self._workflow_triggers.get(trigger.id)
            if (old.version if old else 0) != expected_version:
                raise InvalidTransitionError("workflow trigger changed; reload before retrying")
            self._workflow_triggers[trigger.id] = trigger

    def delete_workflow_trigger(self, identifier: UUID, household_id: UUID, actor_id: UUID) -> bool:
        with self._lock:
            trigger = self._workflow_triggers.get(identifier)
            if not trigger or (trigger.household_id, trigger.actor_id) != (
                household_id,
                actor_id,
            ):
                return False
            del self._workflow_triggers[identifier]
            return True

    def delete_workflow_trigger_for_definition(
        self, definition_id: UUID, household_id: UUID, actor_id: UUID
    ) -> int:
        with self._lock:
            identifiers = [
                item.id
                for item in self._workflow_triggers.values()
                if item.definition_id == definition_id
                and item.household_id == household_id
                and item.actor_id == actor_id
            ]
            for identifier in identifiers:
                del self._workflow_triggers[identifier]
            return len(identifiers)

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
        self,
        household_id: UUID,
        actor_id: UUID,
        run_id: UUID | None = None,
        *,
        thread_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[HomeCommand, ...]:
        with self._lock:
            rows = sorted(
                (
                    c
                    for c in self._home_commands.values()
                    if c.household_id == household_id
                    and c.actor_id == actor_id
                    and (run_id is None or c.run_id == run_id)
                    and (thread_id is None or c.thread_id == thread_id)
                ),
                key=lambda c: (c.created_at, c.id),
                reverse=True,
            )
            return tuple(rows[:limit])

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
