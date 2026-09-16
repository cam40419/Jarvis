import asyncio
import json
from collections.abc import AsyncIterator, Callable
from queue import Empty, Full, Queue
from threading import Event
from uuid import UUID

from fastapi.responses import StreamingResponse

from simon.domain.conversations import Run, SubmitRun
from simon.domain.errors import AuthorizationError, DomainError, ModelError
from simon.domain.models import ActorContext
from simon.services.model_conversations import ModelConversationService


def model_stream(
    service: ModelConversationService,
    actor: ActorContext,
    thread_id: UUID,
    request: SubmitRun,
    revalidate: Callable[[], ActorContext],
) -> StreamingResponse:
    queue: Queue[tuple[str, object]] = Queue(maxsize=16)
    stopped = Event()
    started_run: list[Run] = []

    def emit(kind: str, data: object) -> None:
        while not stopped.is_set():
            try:
                queue.put((kind, data), timeout=0.1)
                return
            except Full:
                continue
        raise ModelError("model_cancelled")

    def started(run: Run) -> None:
        started_run.append(run)
        emit(
            "run.started",
            {"id": str(run.id), "profile": run.profile.model_dump() if run.profile else None},
        )

    def delta(text: str) -> None:
        if stopped.is_set():
            raise ModelError("model_cancelled")
        if started_run:
            attempt = service.store.attempt(started_run[0].id)
            if attempt and attempt.status != "pending":
                raise ModelError(attempt.error_code or "model_cancelled")
        if text:
            emit("text.delta", {"text": text})

    def work() -> None:
        try:
            run = service.submit(
                actor, thread_id, request, revalidate=revalidate, on_delta=delta, on_started=started
            )
            emit("run.completed", run.model_dump(mode="json"))
        except Exception as exc:
            if not stopped.is_set():
                error = exc if isinstance(exc, DomainError) else ModelError()
                emit(
                    "run.error",
                    {
                        "code": error.code,
                        "message": str(error),
                        "reason": error.reason if isinstance(error, ModelError) else None,
                    },
                )

    def check_access() -> None:
        current = revalidate()
        required = {"threads:read", "threads:write"}
        if started_run and started_run[0].memory_context:
            required.add("memories:read")
        if (
            current.actor_id != actor.actor_id
            or current.household_id != actor.household_id
            or not required <= current.scopes
        ):
            raise AuthorizationError("access changed during generation")

    async def events() -> AsyncIterator[str]:
        worker = asyncio.create_task(asyncio.to_thread(work))
        try:
            while True:
                try:
                    kind, data = queue.get_nowait()
                except Empty:
                    await asyncio.sleep(0.02)
                    continue
                try:
                    await asyncio.to_thread(check_access)
                except DomainError:
                    yield (
                        'event: run.error\ndata: {"code":"forbidden",'
                        '"message":"Access changed. Sign in again."}\n\n'
                    )
                    break
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if kind in {"run.completed", "run.error"}:
                    break
        finally:
            stopped.set()
            if started_run:
                await asyncio.shield(asyncio.to_thread(service.cancel, actor, started_run[0].id))
            # The provider thread sees cancellation on its next event or bounded timeout.
            worker.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )
