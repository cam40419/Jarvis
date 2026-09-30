"""Live model with synthetic Drive: no real files are changed."""

import json
import os

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.openai_model import OpenAIModel
from simon.config import Settings
from simon.domain.model import ModelRequest
from simon.services.model_conversations import INSTRUCTIONS
from tests.contract.test_calendar_immediate import pending_calendar
from tests.contract.test_drive_management import setup_management


@pytest.mark.live
def test_live_model_browses_relinks_root_and_trashes_old_folder():
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1")
    settings = Settings()
    assert settings.openai_api_key
    connected, actor, files, project = setup_management(InMemoryStore())
    old = files.binding(actor, project).folder_id
    tools = (
        "google_accounts_list",
        "drive_list_folder",
        "project_list",
        "project_link_drive",
        "project_unlink_drive",
        "project_drive_trash",
        "project_files_list",
    )
    attempt = pending_calendar(connected, actor)
    attempt = attempt.model_copy(
        update={"run": attempt.run.model_copy(update={"capability_manifest": tools})}
    )
    connected.store.save_attempt(attempt)
    calls = []
    execute = connected.executor(actor, attempt.run.id, [], lambda: actor)

    def tracked(name, arguments):
        calls.append(name)
        return execute(name, arguments)

    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_with_tools(
        ModelRequest(
            model=settings.openai_model,
            reasoning_effort="low",
            timeout_seconds=90,
            instructions=INSTRUCTIONS,
            tools=tools,
            input_text=json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": "Use My Drive itself as the Build project's main folder. "
                            "Then delete its old "
                            "Simon - Build folder by moving it to trash. Find the folder yourself.",
                        }
                    ]
                }
            ),
        ),
        lambda delta: None,
        tracked,
    )
    assert files.binding(actor, project).folder_id == "my-drive"
    assert files.api.items[old]["trashed"] and files.api.trashes == 1
    assert "drive_list_folder" in calls
    assert "project_link_drive" in calls and "project_drive_trash" in calls
    assert answer.text
