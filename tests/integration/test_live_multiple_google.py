"""Live model routing against synthetic accounts; never reads real Gmail."""

import json
import os
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.openai_model import OpenAIModel
from simon.config import Settings
from simon.domain.model import ModelRequest
from simon.services.model_conversations import INSTRUCTIONS
from tests.contract.test_connected import connected_setup
from tests.contract.test_multiple_google_accounts import add


@pytest.mark.live
def test_live_model_uses_selected_account():
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1")
    settings = Settings()
    assert settings.openai_api_key
    service, actor, _ = connected_setup(InMemoryStore())
    add(service, actor)
    seen = []

    def search(token, query):
        seen.append(token)
        return {
            "messages": [
                {"id": "testmessage", "subject": "Test meeting", "snippet": "Synthetic result"}
            ],
            "next_page_token": "",
        }

    service.api.gmail_search = search
    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_with_tools(
        ModelRequest(
            model=settings.openai_model,
            reasoning_effort="low",
            timeout_seconds=90,
            instructions=INSTRUCTIONS,
            tools=("google_accounts_list", "gmail_search_messages", "gmail_read_message"),
            input_text=json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": "List unread email subjects in second@example.com. "
                            "Use that account only.",
                        }
                    ]
                }
            ),
        ),
        lambda delta: None,
        service.executor(actor, uuid4(), [], lambda: actor),
    )
    assert seen and set(seen) == {"second-access"}
    assert "Test meeting" in answer.text
