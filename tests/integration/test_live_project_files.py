"""Synthetic file tool selection: never reads or writes real Google files."""

import json
import os

import pytest

from simon.adapters.openai_model import OpenAIModel
from simon.config import Settings
from simon.domain.model import ModelRequest
from simon.services.model_conversations import INSTRUCTIONS


@pytest.mark.live
def test_live_project_request_reads_then_edits_without_confirmation():
    if os.environ.get("SIMON_LIVE_MODEL_TESTS") != "1":
        pytest.skip("set SIMON_LIVE_MODEL_TESTS=1")
    settings = Settings()
    assert settings.openai_api_key
    calls = []
    project_id = "11111111-1111-4111-8111-111111111111"

    def execute(name, arguments):
        args = json.loads(arguments)
        calls.append(name)
        if name == "project_list":
            return json.dumps(
                [{"id": project_id, "subject": "Kitchen", "drive": {"status": "ready"}}]
            )
        if name == "project_files_list":
            assert args["project_id"] == project_id
            return json.dumps(
                {
                    "files": [{"id": "notes", "name": "budget.md", "mimeType": "text/markdown"}],
                    "next_page_token": "",
                }
            )
        if name == "project_file_read":
            assert args["file_id"] == "notes"
            return json.dumps(
                {"id": "notes", "text": "Cabinet allowance: $3,000", "revision": "rev1"}
            )
        assert name == "project_file_edit"
        assert "project_file_read" in calls and args["revision"] == "rev1"
        assert "$3,000" in args["old_text"] and "$4,000" in args["new_text"]
        return json.dumps(
            {
                "status": "succeeded",
                "result": {"id": "notes", "url": "https://drive.google.com/file/d/notes/view"},
            }
        )

    answer = OpenAIModel(settings.openai_api_key.get_secret_value()).generate_with_tools(
        ModelRequest(
            model=settings.openai_model,
            reasoning_effort="low",
            timeout_seconds=90,
            instructions=INSTRUCTIONS,
            tools=(
                "project_list",
                "project_create",
                "project_link_drive",
                "project_sync",
                "project_files_list",
                "project_file_read",
                "project_file_create",
                "project_file_edit",
                "project_sheet_read",
                "project_sheet_write",
                "project_file_rename",
            ),
            input_text=json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": "In the Kitchen project's budget.md, change the cabinet "
                            "allowance from $3,000 to $4,000. Save the file.",
                        }
                    ]
                }
            ),
        ),
        lambda delta: None,
        execute,
    )
    assert calls[-1] == "project_file_edit" and calls.count("project_file_edit") == 1
    assert answer.text and "please confirm" not in answer.text.lower()
