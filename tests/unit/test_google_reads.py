import base64

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.google import ConnectedError
from simon.adapters.model_tools import definitions
from simon.domain.connected_tools import GoogleItem, GoogleSearch
from tests.unit.test_google import adapter


def part(mime, text, **extra):
    return {
        "mimeType": mime,
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")},
        **extra,
    }


def test_gmail_search_pagination_and_multipart_read(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer access"
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == "in:inbox is:unread"
            assert request.url.params["pageToken"] == "page2"
            return httpx.Response(200, json={"messages": [{"id": "abc123"}], "nextPageToken": "3"})
        payload = {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Hello"}],
            "parts": [
                {"mimeType": "multipart/alternative", "parts": [
                    part("text/html", "<p>Duplicate HTML</p>"),
                    part("text/plain", "Actual body"),
                ]},
                part("text/plain", "Private attachment", filename="secret.txt"),
            ],
        }
        return httpx.Response(200, json={"id": "abc123", "payload": payload})

    api = adapter(monkeypatch, respond)
    result = api.gmail_search(
        "access", GoogleSearch(query="in:inbox is:unread", page_token="page2", limit=1)
    )
    assert result["messages"][0]["subject"] == "Hello"
    assert "body" not in result["messages"][0]
    assert result["next_page_token"] == "3"
    message = api.gmail_message("access", GoogleItem(id="abc123"))
    assert message["body"].strip() == "Actual body"
    assert message["body_available"] and not message["attachments_included"]
    assert len(calls) == 3


def test_gmail_html_is_text_and_large_body_is_marked(monkeypatch):
    payload = part(
        "text/html", "<style>hidden</style><p>Hello &amp; welcome</p><script>bad()</script>"
    )
    api = adapter(monkeypatch, lambda req: httpx.Response(200, json={"payload": payload}))
    assert api.gmail_message("a", GoogleItem(id="123"))["body"] == "Hello & welcome"
    payload = part("text/plain", "x" * 40001)
    result = api.gmail_message("a", GoogleItem(id="123"))
    assert result["truncated"] and len(result["body"]) == 40000


def test_drive_search_escapes_query_and_keeps_pagination(monkeypatch):
    def respond(request):
        assert request.url.params["q"] == "trashed = false and fullText contains 'Bob\\'s'"
        assert request.url.params["pageToken"] == "next"
        assert request.url.params["includeItemsFromAllDrives"] == "true"
        return httpx.Response(200, json={
            "files": [{"id": "file_1", "name": "Bob's notes", "mimeType": "text/plain"}],
            "nextPageToken": "last", "incompleteSearch": True,
        })

    result = adapter(monkeypatch, respond).drive_search(
        "a", GoogleSearch(query="Bob's", page_token="next")
    )
    assert result["next_page_token"] == "last" and result["incomplete_search"]
    assert result["files"][0]["url"] == "https://drive.google.com/file/d/file_1/view"


@pytest.mark.parametrize("mime,export", [
    ("application/vnd.google-apps.document", "text/plain"),
    ("application/vnd.google-apps.presentation", "text/plain"),
    ("application/vnd.google-apps.spreadsheet", "text/csv"),
    ("text/plain", None),
    ("application/pdf", False),
])
def test_drive_read_exports_or_returns_explicit_limit(monkeypatch, mime, export):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        if "fields" in request.url.params:
            return httpx.Response(200, json={"id": "file", "name": "Notes", "mimeType": mime})
        if export:
            assert request.url.path.endswith("/file/export")
            assert request.url.params["mimeType"] == export
        else:
            assert request.url.params["alt"] == "media"
        return httpx.Response(200, text="notes" * 9000)

    result = adapter(monkeypatch, respond).drive_file("a", GoogleItem(id="file"))
    if export is False:
        assert "text" not in result and "Metadata only" in result["content_note"]
        assert len(calls) == 1
    else:
        assert len(calls) == 2 and result["truncated"] and len(result["text"]) == 40000
        if export == "text/csv":
            assert "first sheet" in result["content_note"]


@pytest.mark.parametrize("status", [401, 403, 500, 302])
def test_reads_fail_without_leaking_provider_body(monkeypatch, status):
    api = adapter(monkeypatch, lambda req: httpx.Response(status, text="secret provider response"))
    with pytest.raises(ConnectedError) as failure:
        api.gmail_search("secret", GoogleSearch())
    assert "secret" not in str(failure.value)
    if status in {401, 403}:
        assert "reconnect" in str(failure.value)


def test_read_inputs_and_tool_schemas():
    for invalid in ("../token", "https://example.com", "file?alt=media", "a/b"):
        with pytest.raises(ValidationError):
            GoogleItem(id=invalid)
    with pytest.raises(ValidationError):
        GoogleSearch(limit=21)
    schemas = definitions((
        "gmail_search_messages", "gmail_read_message", "drive_search_files", "drive_read_file",
    ))
    assert len(schemas) == 4 and all(schema["strict"] for schema in schemas)
