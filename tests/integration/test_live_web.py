import json
import os

import pytest

from simon.adapters.openai_model import OpenAIModel
from simon.config import Settings
from simon.domain.connected_tools import EmailDraft
from simon.domain.model import ModelRequest


@pytest.mark.live
def test_live_public_web_search():
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1 for the paid synthetic web check")
    settings = Settings()
    assert settings.openai_api_key
    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_stream(
        ModelRequest(
            model="gpt-5.4-mini",
            tools=("web_search",),
            reasoning_effort="none",
            instructions="Use web search to check the official page. Cite its URL. Be brief.",
            input_text="Search the official Python documentation for what pathlib.Path.is_file "
            "does. Give one sentence with a source.",
            timeout_seconds=90,
        ),
        lambda delta: None,
    )
    assert "web_search_call" in answer.tool_calls
    assert answer.web_sources and any(
        source.url.host == "docs.python.org" for source in answer.web_sources
    )
    assert "https://docs.python.org/" in answer.text and answer.input_tokens > 0


@pytest.mark.live
def test_live_function_loop_prepares_only():
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1 for the paid synthetic tool check")
    settings = Settings()
    assert settings.openai_api_key
    previews = []

    def execute(name, arguments):
        assert name == "propose_email"
        previews.append(EmailDraft.model_validate_json(arguments))
        return json.dumps(
            {"executed": False, "requires_confirmation": True, "preview_id": "synthetic-preview"}
        )

    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_with_tools(
        ModelRequest(
            model="gpt-5.4-mini",
            tools=("propose_email",),
            reasoning_effort="low",
            instructions="Use propose_email once to prepare the requested email. The tool does "
            "not send anything. After it succeeds, ask the user to review the card. Be brief.",
            input_text="Prepare an email to robot@example.com, subject Synthetic test, "
            "body exactly: This is a test.",
            timeout_seconds=90,
        ),
        lambda delta: None,
        execute,
    )
    assert len(previews) == 1 and previews[0].to == "robot@example.com"
    assert previews[0].body == "This is a test." and answer.tool_calls == ("propose_email",)
    assert answer.text and answer.input_tokens > 0
