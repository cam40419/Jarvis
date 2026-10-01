"""Opt-in paid check: real model learns a fact and recalls text/voice history."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.conversations import Thread
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.models import utc_now
from simon.domain.voice import VoiceFragment, VoiceSession
from simon.seed import seed_development_identity
from tests.integration.test_restart import running_api


@pytest.mark.postgres
@pytest.mark.live
def test_live_model_saves_personal_context_and_recalls_voice(postgres_url, tmp_path):
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1 for paid synthetic recall check")
    seed_development_identity(postgres_url)
    store = PostgresStore(postgres_url)
    thread = Thread(
        household_id=DEV_HOUSEHOLD_ID,
        created_by=DEV_ACTOR_ID,
        visibility="personal",
        title="Voice workshop discussion",
    )
    store.insert_thread(thread)
    store.save_voice_session(
        VoiceSession(
            household_id=DEV_HOUSEHOLD_ID,
            actor_id=DEV_ACTOR_ID,
            thread_id=thread.id,
            request_key=uuid4(),
            state="closed",
            expires_at=utc_now() + timedelta(minutes=1),
            fragments=(
                VoiceFragment(
                    event_id="test-voice",
                    speaker="user",
                    start_ms=0,
                    end_ms=1000,
                    text="For the Quartz fixture we chose a removable magnetic latch.",
                ),
            ),
        )
    )
    with running_api(postgres_url, tmp_path / "live-recall.log", model_provider="openai") as api:
        origin = str(api.base_url).rstrip("/")
        login = api.post(
            "/auth/dev-login",
            headers={"Origin": origin},
            json={"token": "process-development-secret-32-characters"},
        )
        assert login.status_code == 200
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def ask(text):
            created = api.post(
                "/v1/threads",
                headers=headers,
                json={"title": "Recall check", "idempotency_key": str(uuid4())},
            )
            assert created.status_code == 201
            identifier = created.json()["id"]
            response = api.post(
                f"/v1/threads/{identifier}/runs",
                headers=headers,
                timeout=150,
                json={"text": text, "profile": "quick", "idempotency_key": str(uuid4())},
            )
            assert response.status_code == 201, response.text
            messages = api.get(f"/v1/threads/{identifier}/messages").json()
            return response.json(), messages[-1]["text"]

        run, saved_answer = ask(
            "My ongoing project is named Cedar Atlas. It automates my Bambu A1 plate changer."
        )
        assert "memory_remember" in run["tool_calls"]
        memories = api.get("/v1/memories").json()
        assert memories and all(m["scope"] == "personal" for m in memories), (
            saved_answer,
            run["tool_calls"],
        )
        assert "Cedar Atlas" in str(memories)
        _, answer = ask("What is my ongoing project called, and which printer does it use?")
        assert "Cedar Atlas" in answer and "A1" in answer
        _, answer = ask(
            "What latch did we choose for the Quartz fixture in our earlier voice conversation?"
        )
        assert "magnetic" in answer.lower()
