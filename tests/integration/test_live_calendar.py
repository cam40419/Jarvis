"""Synthetic model-selection check; the executor never calls Google."""

import json
import os

import pytest

from simon.adapters.openai_model import OpenAIModel
from simon.config import Settings
from simon.domain.connected_tools import CalendarDraft
from simon.domain.model import ModelRequest
from simon.services.model_conversations import INSTRUCTIONS


@pytest.mark.live
def test_live_calendar_request_creates_without_preview():
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1 for the synthetic model check")
    settings = Settings()
    assert settings.openai_api_key
    calls = []

    def execute(name, arguments):
        assert name == "calendar_create_event"
        draft = CalendarDraft.model_validate_json(arguments)
        calls.append(draft)
        assert draft.start.isoformat() == "2026-09-29T17:30:00-04:00"
        assert draft.end.isoformat() == "2026-09-29T17:45:00-04:00"
        assert draft.title == "atest event"
        return json.dumps(
            {
                "status": "succeeded",
                "created": True,
                "requires_confirmation": False,
                "calendar": draft.model_dump(mode="json"),
                "url": "https://www.google.com/calendar/event?eid=synthetic",
            }
        )

    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_with_tools(
        ModelRequest(
            model=settings.openai_model,
            tools=("calendar_create_event", "propose_calendar_event"),
            reasoning_effort="low",
            instructions=INSTRUCTIONS,
            input_text=json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": (
                                "Add a test event called atest event today "
                                "at 5:30 pm for 15 minutes."
                            ),
                        }
                    ],
                    "current_time_utc": "2026-09-29T18:00:00Z",
                    "browser_timezone": "America/New_York",
                }
            ),
            timeout_seconds=90,
        ),
        lambda delta: None,
        execute,
    )
    assert len(calls) == 1 and answer.tool_calls == ("calendar_create_event",)
    assert not any(
        phrase in answer.text.lower() for phrase in ("please confirm", "prepared", "review")
    )
    assert answer.text and answer.input_tokens > 0
