import json

import httpx
import pytest

from simon.adapters.project_drive import DOC, FOLDER, DriveError, ProjectDriveAPI
from simon.domain.errors import ValidationError


@pytest.fixture
def transport(monkeypatch):
    calls = []
    replies = []
    original = httpx.Client

    def handle(request):
        calls.append(request)
        status, data = replies.pop(0)
        return httpx.Response(status, json=data)

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs)
    )
    return ProjectDriveAPI(), calls, replies


def test_docs_edits_use_revision_and_selected_tab(transport):
    api, calls, replies = transport
    replies.append((200, {"replies": [{}]}))
    result = api.edit(
        "secret",
        {
            "id": "doc",
            "mimeType": DOC,
            "revision": "revision-one",
            "tabs": [{"tab_id": "tab1", "text": "Original text"}],
        },
        "Original",
        "Edited",
        "tab1",
    )
    assert result["id"] == "doc"
    body = json.loads(calls[0].content)
    assert body["writeControl"] == {"requiredRevisionId": "revision-one"}
    assert body["requests"][0]["replaceAllText"]["tabsCriteria"] == {"tabIds": ["tab1"]}
    assert calls[0].url.host == "docs.googleapis.com"
    with pytest.raises(ValidationError, match="occur once"):
        api.edit(
            "secret",
            {
                "id": "doc",
                "mimeType": DOC,
                "revision": "rev",
                "tabs": [{"tab_id": "tab1", "text": "twice twice"}],
            },
            "twice",
            "once",
            "tab1",
        )
    assert len(calls) == 1


def test_sheet_bounds_and_literal_values(transport):
    api, calls, replies = transport
    with pytest.raises(ValidationError, match="beyond"):
        api.sheet_write("secret", "sheet", "Sheet1!A1", [["one", "two"]])
    with pytest.raises(ValidationError, match="explicit"):
        api.sheet_write("secret", "sheet", "Sheet1!A:A", [["one"]])
    assert not calls
    replies.append((200, {"updatedCells": 2}))
    api.sheet_write("secret", "sheet", "Sheet1!A1:B1", [["=literal", "two"]])
    assert calls[0].url.params["valueInputOption"] == "RAW"
    assert json.loads(calls[0].content)["values"] == [["=literal", "two"]]


@pytest.mark.parametrize("status,unknown", [(403, False), (412, False), (408, True), (503, True)])
def test_write_failures_distinguish_uncertain_results(transport, status, unknown):
    api, calls, replies = transport
    replies.append((status, {"error": {"message": "secret provider detail"}}))
    with pytest.raises(DriveError) as caught:
        api.create("secret", "Folder", None, None, FOLDER, "generated", "operation")
    assert caught.value.unknown is unknown
    assert "secret" not in str(caught.value)
    assert len(calls) == 1


def test_multipart_create_keeps_id_parent_and_operation(transport):
    api, calls, replies = transport
    replies.append((200, {"id": "generated"}))
    api.create("secret", "notes.md", "folder", b"file content", "text/markdown", "generated", "op")
    request = calls[0]
    assert request.url.params["uploadType"] == "multipart"
    assert b'"parents": ["folder"]' in request.content
    assert b'"id": "generated"' in request.content
    assert b'"simonOperation": "op"' in request.content
    assert b"file content" in request.content


def test_folder_search_and_trash_use_drive_api_without_permanent_delete(transport):
    api, calls, replies = transport
    replies.append((200, {"files": [], "nextPageToken": "page-two"}))
    result = api.list_files("secret", "root", "Client's files", folders_only=True, search_all=True)
    assert result["next_page_token"] == "page-two"
    assert "mimeType" in calls[0].url.params["q"] and "in parents" not in calls[0].url.params["q"]
    assert "Client\\'s files" in calls[0].url.params["q"]
    replies.append((200, {"id": "old", "trashed": True}))
    api.trash("secret", {"id": "old", "_etag": "revision-etag", "capabilities": {"canTrash": True}})
    assert calls[1].method == "PATCH"
    assert json.loads(calls[1].content) == {"trashed": True}
    assert calls[1].headers["if-match"] == "revision-etag"
