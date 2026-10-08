"""Persisted browser-independent conversation jobs with bounded worker ownership."""

from contextlib import suppress
from datetime import timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from simon.domain.conversations import Run, SubmitRun
from simon.domain.errors import DomainError, ModelBusyError, ModelError, NotFoundError
from simon.domain.models import ActorContext, Job, JobStatus, utc_now
from simon.services.chat_work import ChatWorkService

KIND = "assistant.session"
ACTIVE = {JobStatus.QUEUED, JobStatus.RUNNING}


class WorkSessionService(ChatWorkService):
    def view(self, job: Job) -> dict[str, Any]:
        return {
            "id": str(job.id),
            "thread_id": job.input["thread_id"],
            "status": job.status.value,
            "text": job.input["request"]["text"],
            "partial_text": (job.result or {}).get("text", ""),
            "run_id": job.input.get("run_id"),
            "error": job.error_code,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }

    def list(self, actor: ActorContext, thread_id: UUID | None = None) -> list[dict[str, Any]]:
        self.authorize(actor)
        if thread_id:
            assert self.conversations
            self.conversations.get(actor, thread_id)
        rows: list[Job] = []
        for offset in range(0, 100000, 500):
            page = self.store.jobs(actor.workspace_id, actor.actor_id, KIND, offset, 500)
            rows.extend(page)
            if len(page) < 500:
                break
        return [
            self.view(job)
            for job in sorted(rows, key=lambda j: j.created_at, reverse=True)
            if (not thread_id or job.input["thread_id"] == str(thread_id))
        ]

    def owned(self, actor: ActorContext, identifier: UUID) -> Job:
        self.authorize(actor)
        job = self.store.get_job(identifier)
        if (
            not job
            or job.kind != KIND
            or (job.workspace_id, job.created_by) != (actor.workspace_id, actor.actor_id)
        ):
            raise NotFoundError("Work session not found.")
        assert self.conversations
        self.conversations.get(actor, UUID(job.input["thread_id"]))
        return job

    def submit(
        self,
        actor: ActorContext,
        thread_id: UUID,
        request: SubmitRun,
    ) -> dict[str, Any]:
        self.authorize(actor, write=True)
        assert self.conversations
        self.conversations.get(actor, thread_id)
        with self.store.transaction(actor.workspace_id):
            previous = self.list(actor, thread_id)
            for item in previous:
                job = self.store.get_job(UUID(item["id"]))
                assert job
                if job.idempotency_key == request.idempotency_key:
                    if job.input["request"] != request.model_dump(mode="json"):
                        raise ModelBusyError("This request key belongs to another message.")
                    return self.view(job)
                if job.status in ACTIVE:
                    raise ModelBusyError(
                        "This conversation is already working. "
                        "Switch conversations or stop it first."
                    )
            pending = self.store.pending_attempt(thread_id)
            if pending and pending.expires_at <= utc_now():
                self.conversations._fail(actor, pending, "model_timeout")
                pending = None
            if pending:
                raise ModelBusyError("This conversation already has an active answer.")
            job, _ = self.jobs.submit(
                actor,
                kind=KIND,
                idempotency_key=request.idempotency_key,
                input={
                    "thread_id": str(thread_id),
                    "request": request.model_dump(mode="json"),
                },
            )
            return self.view(job)

    def cancel(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        self.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self.owned(actor, identifier)
            if job.status in ACTIVE:
                self._cancel_model(actor, job)
                job = self._save(job, job.version, status=JobStatus.CANCELLED)
            return self.view(job)

    def recover(self) -> None:
        for candidate in self.store.jobs_all(KIND, 100, "running"):
            if candidate.updated_at + timedelta(minutes=5) > utc_now():
                continue
            with self.store.transaction(candidate.workspace_id):
                job = self.store.get_job(candidate.id)
                if not job or job.version != candidate.version or job.status != JobStatus.RUNNING:
                    continue
                run_id = job.input.get("run_id")
                attempt = self.store.attempt(UUID(run_id)) if run_id else None
                if attempt and attempt.status == "succeeded":
                    self._save(job, job.version, status=JobStatus.SUCCEEDED, error_code=None)
                elif not run_id:
                    # No provider run was started: safe to put the saved request back in queue.
                    self._save(job, job.version, status=JobStatus.QUEUED)
                else:
                    if attempt and attempt.status == "pending":
                        with suppress(DomainError):
                            actor = self.worker_actor(job)
                            assert self.conversations
                            self.conversations.cancel(actor, UUID(run_id))
                    self._save(
                        job,
                        job.version,
                        status=JobStatus.FAILED,
                        error_code="Interrupted by server restart. Saved progress is available; "
                        "review completed actions before retrying.",
                    )

    def tick(self) -> int:
        self.recover()
        candidates = self.store.jobs_all(KIND, 10)
        if not candidates:
            return 0
        return int(self.execute(candidates[0]))

    def execute(self, candidate: Job) -> bool:
        assert self.conversations
        with self.store.transaction(candidate.workspace_id):
            job = self.store.get_job(candidate.id)
            if not job or job.version != candidate.version or job.status != JobStatus.QUEUED:
                return False
            job = self._save(job, job.version, status=JobStatus.RUNNING)
        try:
            actor = self.worker_actor(job)
            thread_id = UUID(job.input["thread_id"])
            text, last_saved = "", monotonic()

            def current_actor() -> ActorContext:
                current = self.store.get_job(job.id)
                if not current or current.status != JobStatus.RUNNING:
                    raise ModelError("model_cancelled")
                return self.worker_actor(current)

            def started(run: Run) -> None:
                with self.store.transaction(job.workspace_id):
                    current_actor()
                    current = self.store.get_job(job.id)
                    assert current
                    self._save(
                        current, current.version, input={**current.input, "run_id": str(run.id)}
                    )

            def delta(chunk: str) -> None:
                nonlocal text, last_saved
                current_actor()
                text += chunk
                if monotonic() - last_saved >= 1 or not chunk:
                    with self.store.transaction(job.workspace_id):
                        current = self.store.get_job(job.id)
                        if current and current.status == JobStatus.RUNNING:
                            self._save(current, current.version, result={"text": text})
                    last_saved = monotonic()

            request = SubmitRun.model_validate(job.input["request"])
            run = self.conversations.submit(
                actor,
                thread_id,
                request,
                revalidate=current_actor,
                on_started=started,
                on_delta=delta,
            )
            with self.store.transaction(job.workspace_id):
                current = self.store.get_job(job.id)
                if current and current.status == JobStatus.RUNNING:
                    self._save(
                        current,
                        current.version,
                        status=JobStatus.SUCCEEDED,
                        input={**current.input, "run_id": str(run.id)},
                        error_code=None,
                    )
        except Exception as error:
            with self.store.transaction(job.workspace_id):
                current = self.store.get_job(job.id)
                if current and current.status == JobStatus.RUNNING:
                    self._save(
                        current,
                        current.version,
                        status=JobStatus.FAILED,
                        error_code=error.reason
                        if isinstance(error, ModelError)
                        else "Work session failed. Check account access and retry.",
                    )
        return True
