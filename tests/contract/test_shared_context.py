import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from simon.domain.context import CreateMemory, RecallQuery
from simon.domain.conversations import CreateThread, Message, SubmitRun, Thread
from simon.domain.errors import NotFoundError
from simon.domain.identity import Membership
from simon.domain.models import utc_now
from simon.domain.voice import VoiceFragment, VoiceSession
from simon.services.conversations import ConversationService
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_connected import connected_setup
from tests.contract.test_model_runs import FakeModel
from tests.contract.test_voice import delegation, opened, setup_voice, transcript


def history(connected, actor, text, *, title="Project discussion"):
    thread = connected.conversations.create(
        actor, CreateThread(title=title, idempotency_key=str(uuid4()))
    )
    message = Message(thread_id=thread.id, sequence=1, role="user", text=text)
    connected.store.insert_message(message)
    return thread, message


def test_search_existing_text_and_fragmented_voice_is_private_and_paginated(store):
    connected, actor, _ = connected_setup(store)
    thread, message = history(connected, actor, "My printer is a Bambu A1.")
    # A legacy shared thread is still searchable by its original owner.
    old = Thread(workspace_id=actor.workspace_id, created_by=actor.actor_id, title="Legacy")
    store.insert_thread(old)
    store.insert_message(Message(thread_id=old.id, sequence=1, role="user", text="Bambu plates"))
    voice_thread, _ = history(connected, actor, "Do not duplicate backend wrappers")
    voice = VoiceSession(
        workspace_id=actor.workspace_id,
        actor_id=actor.actor_id,
        thread_id=voice_thread.id,
        request_key=uuid4(),
        expires_at=utc_now() + timedelta(minutes=5),
        fragments=tuple(
            VoiceFragment(
                event_id=str(i), speaker="user", text=t, start_ms=i * 100, end_ms=(i + 1) * 100
            )
            for i, t in enumerate(["My Bam", "bu plate swap project is called Atlas."])
        ),
    )
    store.save_voice_session(voice)
    other = actor.model_copy(update={"actor_id": uuid4()})
    store.put_membership(
        Membership(actor_id=other.actor_id, workspace_id=actor.workspace_id, role="owner")
    )
    history(connected, other, "Bambu other account private secret")
    connected.memories.create(
        other,
        CreateMemory(
            subject="Other secret",
            content="Bambu private memory",
            scope="personal",
            idempotency_key=str(uuid4()),
        ),
    )
    result = connected.recall.search(actor, RecallQuery(query="Bambu"), limit=2)
    assert len(result["conversations"]) == 2 and result["next_offset"] == 2
    second = connected.recall.search(actor, RecallQuery(query="Bambu", offset=2))
    assert len(second["conversations"]) == 1
    assert {r["source"] for r in [*result["conversations"], *second["conversations"]]} == {
        "text",
        "voice",
    }
    assert "other account" not in str(result) + str(second)
    assert "Other secret" not in str(result)
    assert "Bambu plate swap" in str(connected.recall.search(actor, RecallQuery(query="Atlas")))
    assert not connected.recall.search(actor, RecallQuery(query="duplicate wrappers"))[
        "conversations"
    ]
    assert not connected.recall.search(actor, RecallQuery(query="nonexistent % _ ' OR 1=1"))[
        "conversations"
    ]
    assert connected.conversations.get(other, old.id) == old
    with pytest.raises(NotFoundError):
        connected.conversations.get(other, thread.id)
    assert thread not in connected.conversations.list(other, 0, 100)
    assert store.recall_documents(uuid4(), actor.actor_id, (), 0, 10) == ()
    assert message.id in {
        UUID(r["id"]) for r in [*result["conversations"], *second["conversations"]]
    }


def test_model_remembers_updates_forgets_and_rejects_unattributed_facts(store):
    connected, actor, _ = connected_setup(store)
    model = FakeModel()
    service = ModelConversationService(store, connected.audit, model, connected.settings, connected)
    thread = service.create(actor, CreateThread(title="Personal", idempotency_key=str(uuid4())))
    saved = []

    def generate(request, on_delta, execute):
        value = json.loads(request.input_text)["messages"][-1]["text"]
        bad = json.loads(
            execute(
                "memory_remember",
                json.dumps(
                    {
                        "subject": "Invented",
                        "content": "Unstated fact",
                        "category": "fact",
                        "evidence": "This was never stated by the user",
                    }
                ),
            )
        )
        assert "error" in bad
        arguments = json.dumps(
            {"subject": "Printer model", "content": value, "category": "fact", "evidence": value}
        )
        result = json.loads(execute("memory_remember", arguments))
        assert result == json.loads(execute("memory_remember", arguments))
        saved.append(result)
        return model.generate(request)

    model.generate_with_tools = generate
    for text in ("My printer is a Bambu A1.", "My printer is now a Bambu P1S."):
        service.submit(actor, thread.id, SubmitRun(text=text, idempotency_key=str(uuid4())))
    assert len(connected.memories.list(actor)) == 1
    assert saved[-1]["supersedes"] == saved[0]["id"]
    assert saved[-1]["source_message_id"]
    assert saved[-1]["scope"] == "personal"
    assert "P1S" in str(connected.recall.search(actor, RecallQuery(query="printer"))["memories"])
    assert "A1" not in str(connected.recall.search(actor, RecallQuery(query="printer"))["memories"])

    def forget(request, on_delta, execute):
        assert (
            json.loads(execute("memory_forget", json.dumps({"memory_id": saved[-1]["id"]})))[
                "accepted"
            ]
            is False
        )
        return model.generate(request)

    model.generate_with_tools = forget
    service.submit(
        actor, thread.id, SubmitRun(text="Forget my printer model", idempotency_key=str(uuid4()))
    )
    assert not connected.memories.list(actor)
    assert not connected.recall.search(actor, RecallQuery(query="printer"))["memories"]
    assert "Bambu" not in str(store.audit_events())


def test_private_memories_never_enter_other_accounts_or_legacy_shared_context(store):
    connected, actor, _ = connected_setup(store)
    private = connected.memories.create(
        actor,
        CreateMemory(
            subject="Project",
            content="Private Atlas project",
            scope="personal",
            category="project",
            idempotency_key=str(uuid4()),
        ),
    )
    other = actor.model_copy(update={"actor_id": uuid4()})
    store.put_membership(
        Membership(actor_id=other.actor_id, workspace_id=actor.workspace_id, role="owner")
    )
    assert connected.memories.list(other) == ()
    with pytest.raises(NotFoundError):
        connected.memories.retract(other, private.id)
    service = ConversationService(store, connected.audit)
    for who, shared in ((actor, False), (actor, True), (other, False)):
        thread = Thread(
            workspace_id=who.workspace_id,
            created_by=who.actor_id,
            title="Boundary",
            visibility="workspace" if shared else "personal",
        )
        store.insert_thread(thread)
        run = service.submit(
            who, thread.id, SubmitRun(text="What do you know?", idempotency_key=str(uuid4()))
        )
        assert bool(run.memory_context) == (who == actor and not shared)


def test_voice_receives_prior_context_and_saves_facts_for_next_text_chat(store):
    async def scenario():
        voice, actor, token, model, _ = setup_voice(store)
        history(voice.connected, actor, "I am building the Atlas plate changer.")
        voice.connected.memories.create(
            actor,
            CreateMemory(
                subject="Preference",
                content="I prefer concise technical answers",
                scope="personal",
                category="preference",
                idempotency_key=str(uuid4()),
            ),
        )
        instructions = []
        original_create = voice.api.create

        async def capture(sdp, prompt, *, voice=None):
            instructions.append(prompt)
            return await original_create(sdp, prompt, voice=voice)

        voice.api.create = capture
        live, _ = await opened(voice, actor, token)
        assert "Atlas plate changer" in instructions[0]
        assert "concise technical answers" in instructions[0]
        assert "Delegate recall questions" in instructions[0]

        def generate(request, on_delta, execute):
            prior = json.loads(execute("context_search", '{"query":"Atlas","offset":0}'))
            assert "Atlas plate changer" in str(prior)
            result = json.loads(
                execute(
                    "memory_remember",
                    json.dumps(
                        {
                            "subject": "Atlas printer",
                            "content": "Atlas uses my Bambu A1.",
                            "category": "project",
                            "evidence": "Atlas uses my Bambu A1.",
                        }
                    ),
                )
            )
            assert result["accepted"]
            return model.generate(request)

        model.generate_with_tools = generate
        await voice.event(live, transcript("Atlas uses my Bambu A1."))
        await voice.event(live, delegation())
        await asyncio.gather(*live.work)
        await voice.shutdown()
        assert "Bambu A1" in str(voice.connected.memories.list(actor))
        recalled = voice.connected.recall.search(actor, RecallQuery(query="Atlas"))
        assert any(r["source"] == "voice" for r in recalled["conversations"])
        model.generate_with_tools = lambda request, on_delta, execute: model.generate(request)
        thread = voice.conversations.create(
            actor, CreateThread(title="Next chat", idempotency_key=str(uuid4()))
        )
        run = voice.conversations.submit(
            actor,
            thread.id,
            SubmitRun(text="What printer does Atlas use?", idempotency_key=str(uuid4())),
        )
        assert "Bambu A1" in str(run.memory_context)
        assert "plate changer" in run.model_request.input_text

    asyncio.run(scenario())


def test_shared_threads_and_revoked_attempts_cannot_use_personal_tools(store):
    connected, actor, token = connected_setup(store)
    model = FakeModel()
    service = ModelConversationService(store, connected.audit, model, connected.settings, connected)
    shared = Thread(workspace_id=actor.workspace_id, created_by=actor.actor_id, title="Old shared")
    store.insert_thread(shared)
    connected.memories.create(
        actor,
        CreateMemory(
            subject="Private",
            content="Private project detail",
            scope="personal",
            idempotency_key=str(uuid4()),
        ),
    )
    run_ids = []

    def generate(request, on_delta, execute):
        assert "context_search" not in request.tools
        assert "memory_remember" not in request.tools
        assert "Private project detail" not in request.input_text
        result = json.loads(execute("context_search", '{"query":"","offset":0}'))
        assert "private conversation" in result["error"]
        return model.generate(request)

    model.generate_with_tools = generate
    service.submit(
        actor,
        shared.id,
        SubmitRun(text="Recall me", idempotency_key=str(uuid4())),
        on_started=lambda run: run_ids.append(run.id),
    )
    executor = connected.executor(actor, run_ids[0], [], lambda: actor)
    assert "active request" in json.loads(executor("context_search", "{}"))["error"]
    connected.store.revoke_sessions(actor.actor_id)
    from simon.domain.errors import AuthenticationError

    with pytest.raises(AuthenticationError):
        connected.executor(actor, run_ids[0], [], lambda: connected.identity.resolve(token)[1])(
            "context_search", "{}"
        )
