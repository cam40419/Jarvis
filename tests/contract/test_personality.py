import asyncio
from uuid import uuid4

import pytest

from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.errors import InvalidTransitionError
from simon.domain.interaction import ResponsePreferences, SavePreferences
from simon.domain.personality import AssistantPersona
from simon.services.interaction import InteractionService
from tests.contract.test_voice import opened, setup_voice


def test_personality_is_personal_preserved_and_snapshotted(store):
    service, actor, token, model, _ = setup_voice(store)
    interaction = InteractionService(store, service.connected.audit)
    request = SavePreferences(
        profile="auto",
        answer_length="auto",
        auto_deep_enabled=True,
        persona=AssistantPersona(
            preset="jarvis",
            address_as="sir",
            voice="vesper",
            instructions="Prefer concise technical explanations.",
        ),
        expected_version=0,
        idempotency_key=str(uuid4()),
    )
    saved = interaction.save_preferences(actor, request)
    assert saved.persona.voice == "vesper"
    assert (
        interaction.preferences(actor.model_copy(update={"actor_id": uuid4()})).persona.preset
        == "simon"
    )
    saved = interaction.save_preferences(
        actor,
        request.model_copy(
            update={
                "persona": None,
                "profile": "quick",
                "expected_version": saved.version,
                "idempotency_key": str(uuid4()),
            }
        ),
    )
    assert saved.persona.preset == "jarvis"  # Old response-settings clients preserve the persona.
    with pytest.raises(InvalidTransitionError):
        interaction.save_preferences(
            actor, request.model_copy(update={"idempotency_key": str(uuid4())})
        )
    thread = service.conversations.create(
        actor, CreateThread(title="Personality", idempotency_key=str(uuid4()))
    )
    service.conversations.submit(
        actor, thread.id, SubmitRun(text="Explain a slicer", idempotency_key=str(uuid4()))
    )
    prompt = model.requests[-1].instructions
    assert "British cadence" in prompt and '"sir"' in prompt
    assert "do not grant tools" in prompt and "name remains Simon" in prompt
    assert model.requests[-1].tools == service.connected.available(actor)

    async def scenario():
        original = service.api.create
        observed = []

        async def create(sdp, instructions, *, voice=None):
            observed.append((voice, instructions))
            return await original(sdp, instructions, voice=voice)

        service.api.create = create
        live, _ = await opened(service, actor, token)
        assert observed[0][0] == "vesper" and "British cadence" in observed[0][1]
        assert live.record.persona == saved.persona and live.record.voice_name == "vesper"
        store.save_response_preferences(actor.workspace_id, actor.actor_id, ResponsePreferences())
        assert live.record.persona.preset == "jarvis"  # Active calls retain their startup snapshot.
        await service.shutdown()

    asyncio.run(scenario())
