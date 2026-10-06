"""Durable execution records; persistence is a controller duty, never a model tool grant."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid5

from simon.domain.agent_platform import PlannedAgentTask
from simon.domain.agent_runs import AgentRun
from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.errors import InvalidTransitionError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, Job, JobStatus, utc_now
from simon.domain.run_journal import JournalEntry, JournalKind, JournalPage, JournalRead
from simon.services.agent_runs import AgentRunService
from simon.services.artifacts import ArtifactStore
from simon.services.canonical import digest
from simon.services.worker_context import ContextLimitError, EvidenceReadError, ToolEvidenceBuffer

JournalWriter = Callable[[JournalKind, int, dict[str, Any]], None]
INDEX_KIND = "platform.run_journal"
ENTRY_KIND = "platform.run_journal_entry"
MAX_ENTRIES = 4096
_SECRET_KEYS = {
    "authorization",
    "accesstoken",
    "refreshtoken",
    "apikey",
    "password",
    "clientsecret",
    "cookie",
    "setcookie",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


class RunJournalService:
    def __init__(self, runs: AgentRunService, *, artifacts: ArtifactStore | None = None) -> None:
        self.runs, self.store = runs, runs.store
        self._artifacts = artifacts

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts or ArtifactStore(self.runs.platform.state_dir / "artifacts")

    @property
    def private(self) -> ArtifactStore:
        return ArtifactStore(self.runs.platform.state_dir / "journal")

    @property
    def evidence(self) -> ArtifactStore:
        return ArtifactStore(self.runs.platform.state_dir / "evidence")

    @staticmethod
    def _index_id(run_id: UUID) -> UUID:
        return uuid5(run_id, "execution-journal")

    def _owned_run(self, actor: ActorContext, project_id: UUID, run_id: UUID) -> AgentRun:
        run = self.runs.get(actor, run_id)
        plan = self.runs.platform.get(actor, run.plan_id)
        if plan.project_id != project_id:
            raise NotFoundError("Project execution record not found")
        return run

    def _record(
        self, actor: ActorContext, run: AgentRun, identifier: UUID
    ) -> tuple[Job, JournalEntry]:
        job = self.store.get_job(identifier)
        if job is None or (job.kind, job.workspace_id, job.created_by) != (
            ENTRY_KIND,
            actor.workspace_id,
            actor.actor_id,
        ):
            raise NotFoundError("Project execution record not found")
        item = JournalEntry.model_validate(job.input["entry"])
        if (
            item.id != identifier
            or item.run_id != run.id
            or (item.artifact.workspace_id, item.artifact.actor_id, item.artifact.run_id)
            != (actor.workspace_id, actor.actor_id, run.id)
            or item.task_id not in {task.id for task in run.tasks}
        ):
            raise NotFoundError("Project execution record not found")
        plan_job = self.store.get_job(run.plan_id)
        if plan_job is None:
            raise NotFoundError("Project execution record not found")
        assigned = (
            next(
                (task for task in plan_job.input["plan"]["tasks"] if task["id"] == item.task_id),
                None,
            )
            if plan_job
            else None
        )
        if (
            not assigned
            or str(item.artifact.task_id) != assigned["task_id"]
            or item.agent_id != assigned["agent_id"]
            or (str(item.project_id) if item.project_id else None)
            != plan_job.input["plan"].get("project_id")
        ):
            raise NotFoundError("Project execution record not found")
        return job, item

    def _writer(
        self,
        actor: ActorContext,
        run_id: UUID,
        task: PlannedAgentTask,
        project_id: UUID | None,
        executor_id: UUID,
    ) -> AgentRun:
        run = self.runs.view(self.runs.job(run_id))
        if (run.workspace_id, run.actor_id, run.executor_id, run.status) != (
            actor.workspace_id,
            actor.actor_id,
            executor_id,
            JobStatus.RUNNING,
        ):
            raise InvalidTransitionError("Execution journal writer is no longer current")
        plan = self.store.get_job(run.plan_id)
        if plan is None:
            raise InvalidTransitionError("Execution journal assignment changed")
        assignment = (
            next((item for item in plan.input["plan"]["tasks"] if item["id"] == task.id), None)
            if plan
            else None
        )
        if (
            not assignment
            or assignment["task_id"] != str(task.task_id)
            or assignment["agent_id"] != task.agent_id
            or (str(project_id) if project_id else None) != plan.input["plan"].get("project_id")
        ):
            raise InvalidTransitionError("Execution journal assignment changed")
        return run

    def _entries(self, actor: ActorContext, run: AgentRun) -> tuple[JournalEntry, ...]:
        index = self.store.get_job(self._index_id(run.id))
        if index is None:
            return ()
        if (index.kind, index.workspace_id, index.created_by) != (
            INDEX_KIND,
            actor.workspace_id,
            actor.actor_id,
        ):
            raise NotFoundError("Project execution record not found")
        identifiers = index.input["entries"]
        if not isinstance(identifiers, list) or len(identifiers) > MAX_ENTRIES:
            raise ArtifactError("Execution journal index is invalid")
        return tuple(self._record(actor, run, UUID(value))[1] for value in identifiers)

    def list_run(
        self, actor: ActorContext, project_id: UUID, run_id: UUID
    ) -> tuple[JournalEntry, ...]:
        run = self._owned_run(actor, project_id, run_id)
        return self._entries(actor, run)

    def list(
        self,
        actor: ActorContext,
        project_id: UUID,
        run_id: UUID,
        *,
        offset: int = 0,
        limit: int = 30,
        kind: JournalKind | None = None,
    ) -> JournalPage:
        if not 0 <= offset <= MAX_ENTRIES or not 1 <= limit <= 100:
            raise ValidationError("Invalid execution journal page")
        items = tuple(
            item
            for item in self.list_run(actor, project_id, run_id)
            if kind is None or item.kind == kind
        )
        end = min(len(items), offset + limit)
        return JournalPage(
            project_id=project_id,
            run_id=run_id,
            items=items[offset:end],
            next_offset=end if end < len(items) else None,
        )

    def entry(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, identifier: UUID
    ) -> JournalEntry:
        run = self._owned_run(actor, project_id, run_id)
        _, item = self._record(actor, run, identifier)
        if item.project_id != project_id:
            raise NotFoundError("Project execution record not found")
        return item

    def read(
        self,
        actor: ActorContext,
        project_id: UUID,
        run_id: UUID,
        identifier: UUID,
        *,
        pointer: str = "",
        offset: int = 0,
        limit: int = 8000,
    ) -> JournalRead:
        item = self.entry(actor, project_id, run_id, identifier)
        storage = (
            self.artifacts
            if item.kind == "candidate"
            else (self.evidence if item.kind == "evidence" else self.private)
        )
        raw = storage.read(item.artifact, max_bytes=8 * 1024 * 1024)
        value = json.loads(raw) if item.artifact.media_type == "application/json" else raw.decode()
        buffer = ToolEvidenceBuffer()
        # Tool evidence is already a bounded captured record. Reuse that exact
        # shape so adding a second wrapper cannot make its last pages unreadable.
        captured = (
            value if item.kind == "evidence" else {"invocation_id": str(item.id), "output": value}
        )
        if not isinstance(captured, dict):
            raise ArtifactError("Saved execution record is invalid")
        try:
            buffer.capture(captured)
        except (ContextLimitError, KeyError, TypeError, ValueError) as error:
            raise ArtifactError("Saved execution record is invalid") from error
        try:
            page = buffer.read(
                {
                    "invocation_id": captured["invocation_id"],
                    "pointer": pointer if item.kind == "evidence" else "/output" + pointer,
                    "offset": offset,
                    "limit": limit,
                }
            )
        except EvidenceReadError as error:
            raise ValidationError(str(error)) from None
        return JournalRead(
            entry=item,
            pointer=pointer,
            offset=page["offset"],
            next_offset=page["next_offset"],
            total_chars=page["total_chars"],
            text=page["text"],
            value_sha256=page["value_sha256"],
            eof=page["eof"],
        )

    def _sanitize(self, value: Any) -> tuple[Any, bool]:
        env = self.runs.platform._environ
        names = {
            entry.api_key_env for entry in self.runs.platform.manifest.models if entry.api_key_env
        }
        names.update(
            entry.credential_env
            for entry in self.runs.platform.manifest.tools
            if entry.credential_env
        )
        names.update(
            entry.credential_env
            for entry in self.runs.platform.manifest.environments
            if entry.credential_env
        )
        names.update(
            key
            for key in env
            if any(marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        )
        secrets = tuple(env[name] for name in names if env.get(name) and len(env[name]) >= 8)
        changed = False

        def clean(item: Any) -> Any:
            nonlocal changed
            if isinstance(item, str):
                for secret in secrets:
                    if secret in item:
                        item = item.replace(secret, "[redacted]")
                        changed = True
                # Controller responses are JSON text, so their structured secret
                # arguments need the same redaction as ordinary object fields.
                if item.lstrip().startswith(("{", "[")):
                    try:
                        decoded = json.loads(item)
                    except (ValueError, RecursionError):
                        pass
                    else:
                        cleaned = clean(decoded)
                        if cleaned != decoded:
                            item = _json(cleaned)
                return item
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    if str(key).lower().replace("_", "").replace("-", "") in _SECRET_KEYS:
                        result[key] = "[redacted]"
                        changed = True
                    else:
                        result[key] = clean(child)
                return result
            if isinstance(item, (tuple, list)):
                return [clean(child) for child in item]
            return item

        result = clean(value)
        return result, changed

    def append(
        self,
        actor: ActorContext,
        run_id: UUID,
        task: PlannedAgentTask,
        *,
        executor_id: UUID,
        project_id: UUID | None,
        kind: JournalKind,
        step: int,
        payload: dict[str, Any],
        artifact: Artifact | None = None,
    ) -> JournalEntry:
        """Trusted dispatcher path, fenced to its claimed run. Never exposed as a write API.

        A returned result is retained even if access was revoked while its call was in flight.
        Public retrieval always rechecks current ownership/project access.
        """
        return self._append(
            actor,
            run_id,
            task,
            validate_writer=lambda: self._writer(actor, run_id, task, project_id, executor_id),
            project_id=project_id,
            kind=kind,
            step=step,
            payload=payload,
            artifact=artifact,
        )

    def _append(
        self,
        actor: ActorContext,
        run_id: UUID,
        task: PlannedAgentTask,
        *,
        validate_writer: Callable[[], AgentRun],
        project_id: UUID | None,
        kind: JournalKind,
        step: int,
        payload: dict[str, Any],
        artifact: Artifact | None = None,
    ) -> JournalEntry:
        identifier = uuid5(task.task_id, f"journal:{kind}:{step}")
        validate_writer()
        if kind == "evidence":
            assert artifact is not None
            identifier = uuid5(task.task_id, f"journal:evidence:{artifact.id}")
        clean, redacted = self._sanitize(payload)
        title, title_redacted = self._sanitize(
            (task.objective.strip().splitlines() or [task.id])[0]
        )
        source_sha = hashlib.sha256(str(payload.get("text", "")).encode()).hexdigest()
        if artifact is None:
            text = clean.get("text", "") if kind == "candidate" else _json(clean)
            if not isinstance(text, str) or len(text) > 2_000_000:
                raise ArtifactError("Execution record exceeds its storage bound")
            storage = self.artifacts if kind == "candidate" else self.private
            artifact = storage.publish_text(
                workspace_id=actor.workspace_id,
                actor_id=actor.actor_id,
                run_id=run_id,
                task_id=task.task_id,
                name=f"{kind}-{step:02d}.{'md' if kind == 'candidate' else 'json'}",
                media_type="text/markdown" if kind == "candidate" else "application/json",
                text=text,
            )
        if (artifact.workspace_id, artifact.actor_id, artifact.run_id, artifact.task_id) != (
            actor.workspace_id,
            actor.actor_id,
            run_id,
            task.task_id,
        ):
            raise ArtifactError("Execution record ownership mismatch")
        item = JournalEntry(
            id=identifier,
            project_id=project_id,
            run_id=run_id,
            task_id=task.id,
            agent_id=task.agent_id,
            kind=kind,
            step=step,
            title=title[:200],
            status="draft" if kind == "candidate" else "recorded",
            artifact=artifact,
            redacted=redacted or title_redacted,
        )
        with self.store.transaction(actor.workspace_id):
            run = validate_writer()
            existing = self.store.get_job(identifier)
            if existing:
                _, saved = self._record(actor, run, identifier)
                if saved.artifact != artifact:
                    raise InvalidTransitionError("Execution record already has different content")
                return saved
            index_id = self._index_id(run_id)
            index = self.store.get_job(index_id)
            identifiers = list(index.input["entries"]) if index else []
            if len(identifiers) >= MAX_ENTRIES:
                raise ArtifactError("Execution journal entry limit reached")
            self.store.create_job(
                Job(
                    id=identifier,
                    workspace_id=actor.workspace_id,
                    created_by=actor.actor_id,
                    kind=ENTRY_KIND,
                    status=JobStatus.SUCCEEDED,
                    idempotency_key=identifier.hex,
                    input={"entry": item.model_dump(mode="json"), "source_sha256": source_sha},
                    input_digest=digest(item.model_dump(mode="json")),
                )
            )
            identifiers.append(str(identifier))
            values = {
                "project_id": str(project_id) if project_id else None,
                "run_id": str(run_id),
                "entries": identifiers,
            }
            if index:
                self.store.save_job(
                    index.model_copy(update={"input": values, "updated_at": utc_now()}),
                    index.version,
                )
            else:
                self.store.create_job(
                    Job(
                        id=index_id,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind=INDEX_KIND,
                        status=JobStatus.SUCCEEDED,
                        idempotency_key=index_id.hex,
                        input=values,
                        input_digest=digest({"run_id": str(run_id)}),
                    )
                )
            if kind == "review":
                for candidate in self._entries(actor, run):
                    if candidate.kind != "candidate" or candidate.task_id != task.id:
                        continue
                    job, _ = self._record(actor, run, candidate.id)
                    if job.input["source_sha256"] == payload.get("candidate_sha256"):
                        candidate = candidate.model_copy(
                            update={
                                "status": "accepted"
                                if payload.get("status") == "complete"
                                else "partial"
                            }
                        )
                        self.store.save_job(
                            job.model_copy(
                                update={
                                    "input": {
                                        **job.input,
                                        "entry": candidate.model_dump(mode="json"),
                                    },
                                    "updated_at": utc_now(),
                                }
                            ),
                            job.version,
                        )
        return item

    def backfill(
        self,
        actor: ActorContext,
        project_id: UUID,
        run_id: UUID,
        *,
        offset: int = 0,
        limit: int = 20,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Explicit maintenance only: retain settled legacy outputs without replaying work.

        The old run, task statuses, output and artifacts are never changed. An uncertain
        task's text remains partial and does not reconcile any uncertain action.
        """
        self.runs.platform.authorize(actor, write=True)
        if (
            type(offset) is not int
            or not 0 <= offset <= 100
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValidationError("Invalid legacy output backfill page")

        def settled() -> AgentRun:
            self.runs.platform.authorize(actor, write=True)
            current = self._owned_run(actor, project_id, run_id)
            if (
                current.status
                not in {
                    JobStatus.SUCCEEDED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.NEEDS_HUMAN,
                }
                or current.reserved_slots
                or any(task.status in {"queued", "running"} for task in current.tasks)
            ):
                raise InvalidTransitionError("Only settled, unreserved runs can be backfilled")
            # A finished run retains its executor ID for provenance. Its terminal
            # status fences live writers; reservations and task states show whether
            # execution is settled without rewriting that historical identity.
            return current

        original = settled()
        planned = {task.id: task for task in self.runs.platform.get(actor, original.plan_id).tasks}
        created = 0
        eligible = 0
        end = min(len(original.tasks), offset + limit)
        for task in original.tasks[offset:end]:
            if not task.output or (task.status == "succeeded" and task.artifacts):
                continue
            with self.store.transaction(actor.workspace_id):
                current = settled()
                if current.version != original.version:
                    raise InvalidTransitionError("The saved run changed during output backfill")
                candidates = [
                    item
                    for item in self._entries(actor, current)
                    if item.kind == "candidate" and item.task_id == task.id
                ]
                if candidates:
                    continue
                assignment = planned.get(task.id)
                if assignment is None:
                    raise InvalidTransitionError("The saved task assignment is unavailable")
                eligible += 1
                if dry_run:
                    continue
                self._append(
                    actor,
                    run_id,
                    assignment,
                    validate_writer=settled,
                    project_id=project_id,
                    kind="candidate",
                    step=0,
                    payload={"text": task.output},
                )
                self._append(
                    actor,
                    run_id,
                    assignment,
                    validate_writer=settled,
                    project_id=project_id,
                    kind="review",
                    step=0,
                    payload={
                        "status": "complete" if task.status == "succeeded" else "partial",
                        "reviewed": False,
                        "legacy_backfill": True,
                        "original_task_status": task.status,
                        "candidate_sha256": hashlib.sha256(task.output.encode()).hexdigest(),
                    },
                )
                created += 1
        return {
            "run_id": str(run_id),
            "scanned": max(0, end - offset),
            "created": created,
            "eligible": eligible,
            "dry_run": dry_run,
            "next_offset": end if end < len(original.tasks) else None,
        }
