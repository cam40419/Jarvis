"""Real model, synthetic local ZIP in a temporary directory; no personal files or Google writes."""

import json
import os
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.openai_model import OpenAIModel
from simon.config import Settings
from simon.domain.model import ModelRequest
from simon.services.model_conversations import INSTRUCTIONS
from tests.contract.test_local_files import local_setup, zip_bytes


@pytest.mark.live
def test_live_local_zip_extraction_and_edit(tmp_path):
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1")
    settings = Settings()
    assert settings.openai_api_key
    _, actor, files, host = local_setup(InMemoryStore(), tmp_path)
    (host / "Stdout Collective.zip").write_bytes(zip_bytes([("notes.md", "Status: Draft")]))
    calls = []

    def execute(name, arguments):
        calls.append(name)
        return json.dumps(
            files.run(actor, name, json.loads(arguments), str(uuid4()), lambda: actor)
        )

    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_with_tools(
        ModelRequest(
            model=settings.openai_model,
            reasoning_effort="low",
            timeout_seconds=90,
            instructions=INSTRUCTIONS,
            tools=(
                "local_files_roots",
                "local_files_list",
                "local_files_search",
                "local_file_read",
                "local_zip_inspect",
                "local_zip_extract",
                "local_file_edit",
                "local_file_write",
                "local_file_move",
                "local_folder_create",
                "local_zip_create",
                "local_file_import_drive",
                "local_file_export_drive",
            ),
            input_text=json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": "Unzip Stdout Collective.zip from Downloads into a new Stdout "
                            "folder in your workspace. Read the extracted notes.md and change "
                            "Status: Draft to Status: Ready. Save it.",
                        }
                    ]
                }
            ),
        ),
        lambda delta: None,
        execute,
    )
    assert "local_zip_extract" in calls
    assert "local_file_edit" in calls or "local_file_write" in calls
    assert (files.workspace(actor) / "Stdout" / "notes.md").read_text() == "Status: Ready"
    assert answer.text and "please confirm" not in answer.text.lower()
