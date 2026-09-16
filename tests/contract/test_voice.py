import asyncio
import json
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from simon.domain.errors import AuthorizationError, ModelBusyError, ModelError, NotFoundError
from simon.domain.models import utc_now
from simon.domain.voice import VoiceOffer
from simon.services.model_conversations import ModelConversationService
from simon.services.voice import VoiceService
from tests.contract.test_connected import connected_setup
from tests.contract.test_model_runs import FakeModel


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def recv(self):
        return json.dumps(await self.incoming.get())

    async def send(self, message):
        event = json.loads(message)
        self.sent.append(event)
        if event["type"] == "session.close":
            await self.incoming.put({"type": "session.closed", "usage": {"seconds": 12.5}})

    async def close(self):
        self.closed = True


def setup_voice(store):
    connected, actor, token = connected_setup(store)
    connected.settings.openai_api_key = SecretStr("synthetic-voice-key")
    model = FakeModel()
    model.generate_with_tools = lambda request, on_delta, execute: model.generate(request)
    conversations = ModelConversationService(
        store, connected.audit, model, connected.settings, connected
    )
    service = VoiceService(connected, conversations)
    socket = Socket()

    async def create(sdp, instructions):
        assert "Backend tools:" in instructions
        return "live_private_provider_id", "v=0\r\nanswer"

    async def attach(identifier):
        assert identifier == "live_private_provider_id"
        return socket

    service.api.create, service.api.attach = create, attach
    return service, actor, token, model, socket


def offer():
    return VoiceOffer(sdp="v=0\r\nsynthetic", idempotency_key=uuid4())


async def opened(service, actor, token, request=None):
    result = await service.start(actor, token, request or offer())
    return service.active[UUID(result["id"])], result


def transcript(text="Find the weather in Boston", end=100, speaker="input", identifier=None):
    return {
        "type": f"session.{speaker}_transcript.delta",
        "event_id": identifier or str(uuid4()),
        "delta": text,
        "start_ms": max(0, end - 100),
        "end_ms": end,
    }


def delegation(identifier="item_one", **extra):
    return {
        "type": "session.delegation.created",
        "delegation": {"id": identifier, "target": "client", **extra},
    }


def test_voice_delegates_only_trusted_events_and_persists_usage(store):
    async def scenario():
        service, actor, token, model, socket = setup_voice(store)
        live, response = await opened(service, actor, token)
        assert "provider_id" not in response and "synthetic-voice-key" not in str(response)
        event = transcript()
        await service.event(live, event)
        await service.event(live, event)
        await service.event(live, transcript("Checking now.", end=120, speaker="output"))
        await service.event(live, {"type": "session.input_audio", "audio": "DO_NOT_STORE"})
        assert len(live.record.fragments) == 2 and not model.requests
        await service.event(live, delegation(target="responses"))
        assert not live.work
        await service.event(live, delegation("item_" + "x" * 190))
        await service.event(live, delegation("item_" + "x" * 190))
        await asyncio.gather(*live.work)
        assert len(model.requests) == 1
        assert "Boston" in str(model.requests[0])
        assert socket.sent[-1]["content"] == "A useful answer."
        assert socket.sent[-1]["delegation_id"] == "item_" + "x" * 190
        assert live.record.backend_status == "Result ready in the conversation"
        for seconds in (10, 11, 11):
            await service.event(
                live, {"type": "session.usage.updated", "usage": {"seconds": seconds}}
            )
        assert live.record.seconds == 11
        await service.shutdown()
        saved = service.get(actor, live.record.id)
        assert saved.usage_final and saved.seconds == 12.5 and saved.state == "closed"
        assert socket.closed and not service.active
        assert "DO_NOT_STORE" not in saved.model_dump_json()
        assert store.voice_sessions(actor.household_id, uuid4()) == ()
        assert store.voice_session(uuid4(), saved.id) is None
        with pytest.raises(NotFoundError):
            service.get(actor.model_copy(update={"actor_id": uuid4()}), saved.id)
        with pytest.raises(RuntimeError), store.transaction(actor.household_id):
            store.save_voice_session(saved.model_copy(update={"seconds": 99}))
            raise RuntimeError("rollback")
        assert store.voice_session(actor.household_id, saved.id).seconds == 12.5

    asyncio.run(scenario())


def test_voice_admission_duplicate_rate_limit_and_stale_lease(store):
    async def scenario():
        service, actor, token, _, _ = setup_voice(store)
        request = offer()
        live, _ = await opened(service, actor, token, request)
        with pytest.raises(ModelBusyError):
            await opened(service, actor, token, request)
        with pytest.raises(ModelBusyError):
            await opened(service, actor, token)
        await service.close(live)
        with pytest.raises(ModelBusyError):
            await opened(service, actor, token, request)
        # Old process lease recovery uses the stored deadline, without replaying any task.
        stale = live.record.model_copy(
            update={"state": "active", "expires_at": utc_now() - timedelta(seconds=1)}
        )
        store.save_voice_session(stale)
        live2, _ = await opened(service, actor, token)
        assert store.voice_session(actor.household_id, stale.id).state == "failed"
        await service.close(live2)
        live3, _ = await opened(service, actor, token)
        await service.close(live3)
        with pytest.raises(ModelBusyError):
            await opened(service, actor, token)
        service.settings.voice_enabled = False
        with pytest.raises(ModelError):
            await opened(service, actor, token)

    asyncio.run(scenario())


def test_voice_cancellation_discards_late_model_result(store):
    async def scenario():
        service, actor, token, model, socket = setup_voice(store)
        entered, release = Event(), Event()
        model.action = lambda: (entered.set(), release.wait(8))
        live, _ = await opened(service, actor, token)
        try:
            await service.event(live, transcript())
            await service.event(live, delegation())
            assert await asyncio.to_thread(entered.wait, 5)
            run_id = live.run_id
            await service.stop_work(live)
            release.set()
            await asyncio.gather(*live.work)
            assert store.attempt(run_id).status == "failed"
            assert not [e for e in socket.sent if e["type"] == "session.commentary.append"]
            with pytest.raises(AuthorizationError):
                service.check(live, live.generation - 1)
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_voice_failure_empty_context_auth_and_watchdog(store):
    async def scenario():
        service, actor, token, model, socket = setup_voice(store)
        live, _ = await opened(service, actor, token)
        await service.event(live, delegation())
        await asyncio.gather(*live.work)
        assert not model.requests
        await service.event(live, transcript("x" * 3000))
        model.action = lambda: (_ for _ in ()).throw(ModelError())
        await service.event(live, delegation("item_fail"))
        await asyncio.gather(*live.work)
        assert "Do not claim success" in socket.sent[-1]["content"]
        await service.event(live, {"type": "error", "secret": "hidden"})
        assert "hidden" not in live.record.backend_status
        live.last_heartbeat -= 40
        await asyncio.wait_for(live.finished.wait(), 6)
        await service.shutdown()
        assert not service.active
        assert live.record.state == "closed"  # Provider acknowledged termination.
        live2, _ = await opened(service, actor, token)
        service.save(live2, expires_at=utc_now() - timedelta(seconds=1))
        with pytest.raises(AuthorizationError):
            service.check(live2)
        await service.shutdown()

    asyncio.run(scenario())


def test_voice_start_failure_cleanup_and_transcript_limit(store):
    async def scenario():
        service, actor, token, _, socket = setup_voice(store)
        attach = service.api.attach

        async def failed(identifier):
            service.api.attach = attach
            raise OSError("provider failure with private data")

        service.api.attach = failed
        with pytest.raises(ModelError) as error:
            await opened(service, actor, token)
        assert "private" not in str(error.value) and socket.closed and not service.active
        saved = store.voice_sessions(actor.household_id, actor.actor_id)[0]
        assert saved.state == "failed" and not saved.usage_final
        live, _ = await opened(service, actor, token)
        # New socket in real calls; queued closed event from the failed attach is not relevant.
        while not socket.incoming.empty():
            socket.incoming.get_nowait()
        for i in range(13):
            await service.event(live, transcript("x" * 8000, end=i + 100))
        assert "length limit" in live.record.error
        await service.shutdown()

    asyncio.run(scenario())
