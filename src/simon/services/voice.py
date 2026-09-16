"""Bounded live calls; only provider-authenticated sideband events can delegate work."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from simon.adapters.live_voice import LiveVoiceAPI, VoiceSocket
from simon.domain.conversations import CreateThread, Run, SubmitRun
from simon.domain.errors import AuthorizationError, ModelBusyError, ModelError, NotFoundError
from simon.domain.models import ActorContext, utc_now
from simon.domain.voice import VoiceFragment, VoiceOffer, VoiceSession
from simon.services.connected import ConnectedService
from simon.services.identity import IDENTITY_LOCK
from simon.services.model_conversations import ModelConversationService

VOICE_INSTRUCTIONS = (
    "You are Simon, a friendly, concise personal voice assistant. Speak naturally in short "
    "responses. Explain that your voice is AI-generated if asked. "
    "Backchannel policy: Use brief, occasional acknowledgments. "
    "Interruption policy: Yield when the user interrupts; listen to their correction. "
    "Delegation policy: Backend tools: {tools}. Delegate when the user requests one of these "
    "capabilities, needs current facts, asks a complex question, or changes/cancels an active "
    "task. Wait for their complete request before delegating. Do not delegate greetings or "
    "repeat completed actions without a fresh request. Never claim a task succeeded until the "
    "backend reports success. Device changes need no confirmation. Email/calendar writes "
    "require review cards in the linked conversation; spoken approval does not send them. "
    "When the user cancels a task, delegate the cancellation and explain that an action already "
    "sent cannot be undone merely by interrupting speech."
)


@dataclass
class ActiveVoice:
    record: VoiceSession
    actor: ActorContext
    token: str = field(repr=False)
    timezone: str = "UTC"
    socket: VoiceSocket | None = None
    reader: asyncio.Task[None] | None = None
    watch: asyncio.Task[None] | None = None
    work: set[asyncio.Task[None]] = field(default_factory=set)
    seen_events: set[str] = field(default_factory=set)
    seen_delegations: set[str] = field(default_factory=set)
    generation: int = 0
    run_id: UUID | None = None
    last_heartbeat: float = field(default_factory=monotonic)
    last_saved: float = 0
    last_request_end: int = -1
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    closing: bool = False


class VoiceService:
    def __init__(self, connected: ConnectedService, conversations: ModelConversationService | None):
        self.connected, self.conversations = connected, conversations
        self.store, self.settings = connected.store, connected.settings
        self.api = LiveVoiceAPI(self.settings)
        self.active: dict[UUID, ActiveVoice] = {}

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.voice_enabled and self.settings.openai_api_key and self.conversations
        )

    def get(self, actor: ActorContext, identifier: UUID) -> VoiceSession:
        self.connected.conversations.authorize(actor, "threads:read")
        record = self.store.voice_session(actor.household_id, identifier)
        if not record or record.actor_id != actor.actor_id:
            raise NotFoundError("voice session not found")
        self.connected.conversations.get(actor, record.thread_id)
        if record.state == "active" and identifier not in self.active:
            # Stop the browser after a process restart. Keep the durable admission lease
            # until its deadline because final provider usage cannot be confirmed here.
            return record.model_copy(
                update={
                    "state": "failed",
                    "error": "Simon restarted. This voice connection has ended.",
                }
            )
        return record

    def check(self, live: ActiveVoice, generation: int | None = None) -> ActorContext:
        _, actor = self.connected.identity.resolve(live.token)
        if (actor.actor_id, actor.household_id) != (live.actor.actor_id, live.actor.household_id):
            raise AuthorizationError("Voice access changed")
        self.connected.conversations.authorize(actor, "threads:write")
        self.connected.conversations.get(actor, live.record.thread_id)
        if live.record.state not in {"starting", "active"} or live.record.expires_at <= utc_now():
            raise AuthorizationError("Voice session ended")
        if generation is not None and generation != live.generation:
            raise AuthorizationError("Voice task was superseded")
        return actor

    def save(self, live: ActiveVoice, **update: Any) -> None:
        live.record = live.record.model_copy(update=update)
        with self.store.transaction(live.actor.household_id):
            self.store.save_voice_session(live.record)
        live.last_saved = monotonic()

    async def start(self, actor: ActorContext, token: str, offer: VoiceOffer) -> dict[str, Any]:
        if not self.enabled:
            raise ModelError("model_not_configured")
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            _, checked = self.connected.identity.resolve(token)
            if (checked.actor_id, checked.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Voice access changed")
            self.connected.conversations.authorize(checked, "threads:write")
            previous = self.store.voice_sessions(actor.household_id, actor.actor_id)
            for old in previous:
                if old.request_key == offer.idempotency_key:
                    raise ModelBusyError(
                        "This voice connection was already requested. End it before starting again."
                    )
                if old.state in {"starting", "active", "closing"}:
                    if old.expires_at > utc_now():
                        raise ModelBusyError(
                            "End your existing voice conversation before starting another."
                        )
                    self.store.save_voice_session(
                        old.model_copy(update={"state": "failed", "error": "Session expired"})
                    )
            if sum(s.created_at > utc_now() - timedelta(minutes=1) for s in previous) >= 3:
                raise ModelBusyError(
                    "Please wait a minute before starting another voice conversation."
                )
            thread = self.connected.conversations.create(
                checked,
                CreateThread(
                    title="Voice · " + utc_now().strftime("%b %d %H:%M"),
                    idempotency_key="voice:" + str(offer.idempotency_key),
                ),
            )
            record = VoiceSession(
                household_id=actor.household_id,
                actor_id=actor.actor_id,
                thread_id=thread.id,
                request_key=offer.idempotency_key,
                expires_at=utc_now() + timedelta(seconds=self.settings.voice_max_seconds),
            )
            self.store.save_voice_session(record)
        live = ActiveVoice(record, actor, token, offer.timezone)
        self.active[record.id] = live
        try:
            tools = ", ".join(self.connected.available(actor)) or "general reasoning"
            identifier, answer = await self.api.create(
                offer.sdp, VOICE_INSTRUCTIONS.format(tools=tools)
            )
            self.save(live, provider_id=identifier)
            live.socket = await self.api.attach(identifier)
            self.check(live)
            self.save(live, state="active")
            live.reader = asyncio.create_task(self.listen(live))
            live.watch = asyncio.create_task(self.watch(live))
            return {
                "id": str(record.id),
                "thread_id": str(thread.id),
                "sdp": answer,
                "max_seconds": self.settings.voice_max_seconds,
            }
        except BaseException as exc:
            if live.record.provider_id and not live.socket:
                with suppress(Exception):
                    live.socket = await self.api.attach(live.record.provider_id)
            await self.close(
                live, error="Voice could not connect. Check model access and try again."
            )
            if isinstance(exc, (ModelError, asyncio.CancelledError)):
                raise
            raise ModelError("model_unavailable") from None

    async def listen(self, live: ActiveVoice) -> None:
        assert live.socket
        try:
            while not live.finished.is_set():
                event = json.loads(await live.socket.recv())
                await self.event(live, event)
        except Exception:
            if not live.finished.is_set():
                await self.close(live, error="Voice connection was lost. Start a new conversation.")

    async def event(self, live: ActiveVoice, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            fragment = VoiceFragment(
                event_id=event["event_id"],
                speaker="user" if kind == "session.input_transcript.delta" else "assistant",
                text=event["delta"],
                start_ms=event["start_ms"],
                end_ms=event["end_ms"],
            )
            if fragment.event_id in live.seen_events:
                return
            if (
                len(live.record.fragments) >= 3900
                or sum(len(f.text) for f in live.record.fragments) + len(fragment.text) > 100000
            ):
                await self.close(live, error="Conversation length limit reached. Start a new call.")
                return
            live.seen_events.add(fragment.event_id)
            live.record = live.record.model_copy(
                update={"fragments": (*live.record.fragments, fragment)}
            )
        elif kind in {"session.usage.updated", "session.closed"}:
            seconds = event.get("usage", {}).get("seconds", live.record.seconds)
            if isinstance(seconds, (int, float)) and 0 <= seconds <= 86400:
                live.record = live.record.model_copy(update={"seconds": seconds})
            if kind == "session.closed":
                self.save(live, state="closed", usage_final=True, backend_status="Call ended")
                live.finished.set()
                await self.stop_work(live)
        elif kind == "session.delegation.created" and live.record.state == "active":
            delegation = event.get("delegation", {})
            identifier = delegation.get("id")
            if (
                delegation.get("target") != "client"
                or not isinstance(identifier, str)
                or len(identifier) > 200
            ):
                return
            if identifier in live.seen_delegations:
                return
            if len(live.seen_delegations) >= 60:
                await self.close(live, error="Voice task limit reached. Start a new call.")
                return
            live.seen_delegations.add(identifier)
            await self.stop_work(live)
            if len(live.work) >= 3:
                self.save(
                    live, backend_status="Previous tasks are stopping. Please wait, then ask again."
                )
                assert live.socket
                await self.api.send(
                    live.socket,
                    {
                        "type": "session.commentary.append",
                        "event_id": str(uuid4()),
                        "delegation_id": identifier,
                        "content": "Previous tasks are still stopping. "
                        "Ask the user to wait and repeat their request.",
                    },
                )
                return
            generation = live.generation
            task = asyncio.create_task(self.delegate(live, identifier, generation))
            live.work.add(task)
            task.add_done_callback(live.work.discard)
        elif kind == "error":
            self.save(
                live, backend_status="Voice service reported an error. Please repeat or reconnect."
            )
        if monotonic() - live.last_saved > 1:
            self.save(live)

    async def stop_work(self, live: ActiveVoice) -> None:
        live.generation += 1
        if live.run_id and self.conversations:
            with suppress(Exception):
                await asyncio.to_thread(self.conversations.cancel, live.actor, live.run_id)
        live.run_id = None

    async def delegate(self, live: ActiveVoice, identifier: str, generation: int) -> None:
        assert self.conversations and live.socket
        # Delegation, not transcript timing, authorizes starting a backend task. Let already
        # in-flight transcript events arrive before taking the bounded context snapshot.
        await asyncio.sleep(0.25)
        try:
            self.check(live, generation)
            fragments = live.record.fragments
            new = [f for f in fragments if f.speaker == "user" and f.end_ms > live.last_request_end]
            if not new:
                return
            text = "".join(f.text for f in new)
            if len(text) > 2800:
                text = text[-2800:]
            prior = "".join(f"{f.speaker}: {f.text}\n" for f in fragments if f not in new)[-650:]
            live.last_request_end = max(f.end_ms for f in new)
            request = SubmitRun(
                text="Voice request (transcription may contain mistakes). Act only on the latest "
                "user request; earlier text is context, not new instructions. If this cancels "
                "a previous task, check receipts before reporting what was stopped.\n"
                + "Earlier conversation:\n"
                + prior
                + "\nLatest user request:\n"
                + text,
                idempotency_key="voice:"
                + str(live.record.id)
                + ":"
                + sha256(identifier.encode()).hexdigest(),
                profile="auto",
                answer_length="brief",
                timezone=live.timezone,
            )
            self.save(live, backend_status="Working on your request")

            def started(run: Run) -> None:
                self.check(live, generation)
                live.run_id = run.id

            run = await asyncio.to_thread(
                self.conversations.submit,
                live.actor,
                live.record.thread_id,
                request,
                on_started=started,
                revalidate=lambda: self.check(live, generation),
            )
            self.check(live, generation)
            messages = self.store.recent_messages(live.record.thread_id, 2)
            reply = next(m.text for m in messages if m.id == run.output_message_id)
            await self.api.send(
                live.socket,
                {
                    "type": "session.commentary.append",
                    "event_id": str(uuid4()),
                    "delegation_id": identifier,
                    "content": reply[:2500],
                },
            )
            self.save(live, backend_status="Result ready in the conversation")
        except Exception:
            if generation == live.generation and live.record.state == "active":
                self.save(
                    live,
                    backend_status="Task incomplete. Check conversation results before retrying.",
                )
                with suppress(Exception):
                    await self.api.send(
                        live.socket,
                        {
                            "type": "session.commentary.append",
                            "event_id": str(uuid4()),
                            "delegation_id": identifier,
                            "content": "The task did not finish. "
                            "Do not claim success or retry it automatically. "
                            "The user can inspect any device receipts in the conversation.",
                        },
                    )

    async def watch(self, live: ActiveVoice) -> None:
        try:
            while not live.finished.is_set():
                await asyncio.sleep(2)
                self.check(live)
                if monotonic() - live.last_heartbeat > 35:
                    raise AuthorizationError("Browser disconnected")
                self.save(live)
        except Exception:
            await self.close(
                live, error="Voice ended after inactivity, expiry, or an access change."
            )
        finally:
            if live.finished.is_set():
                await self.close(live)

    async def close(self, live: ActiveVoice, error: str | None = None) -> None:
        if live.record.id not in self.active or live.closing:
            return
        live.closing = True
        already_final = live.record.usage_final
        if not already_final:
            self.save(live, state="closing", error=error, backend_status="Ending call")
        await self.stop_work(live)
        if live.socket and not already_final:
            with suppress(Exception):
                await self.api.send(live.socket, {"type": "session.close"})
            if live.reader and live.reader is not asyncio.current_task():
                with suppress(TimeoutError):
                    await asyncio.wait_for(live.finished.wait(), timeout=5)
        if not live.record.usage_final:
            self.save(
                live,
                state="failed" if error else "closed",
                backend_status="Call ended",
                error=error or "Final voice usage was not confirmed.",
            )
        live.finished.set()
        if live.socket:
            with suppress(Exception):
                await live.socket.close()
        for task in (live.reader, live.watch):
            if task and task is not asyncio.current_task():
                task.cancel()
        self.active.pop(live.record.id, None)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(live) for live in list(self.active.values())))
