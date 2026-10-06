from __future__ import annotations

import base64
import json
from uuid import uuid4

import httpx
import pytest

from simon.adapters.cloud_storage_tools import (
    BoxTransport,
    DropboxTransport,
    OneDriveTransport,
    box_tool_definitions,
    cloud_storage_tool_status,
    dropbox_tool_definitions,
    onedrive_tool_definitions,
)
from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError

SECRET = "synthetic-cloud-access-token"


def setup(provider="dropbox", operation="list", respond=None):
    actor, workspace = uuid4(), uuid4()
    common = {"enabled": True, "workspace_id": workspace, "actor_ids": [actor]}
    if provider == "dropbox":
        definitions = dropbox_tool_definitions(**common, root_path="/Simon")
        cls, key = DropboxTransport, "SIMON_DROPBOX_TOKEN"
    elif provider == "box":
        definitions = box_tool_definitions(**common, root_folder_id="10")
        cls, key = BoxTransport, "SIMON_BOX_TOKEN"
    else:
        definitions = onedrive_tool_definitions(
            **common,
            drive_id="drive",
            root_item_id="root",
            download_hosts=["tenant.sharepoint.com"],
        )
        cls, key = OneDriveTransport, "SIMON_ONEDRIVE_TOKEN"
    definition = next(item for item in definitions if item.id == provider + "." + operation)
    context = ToolExecutionContext(
        actor_id=actor,
        workspace_id=workspace,
        run_id=uuid4(),
        agent_id="files",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=definition.required_scopes,
        authorized_action=definition.action_policy,
    )
    calls = []

    def handler(request):
        calls.append(request)
        return respond(request) if respond else httpx.Response(500)

    transport = cls(transport=httpx.MockTransport(handler), environ={key: SECRET})
    return definition, context, transport, calls


def drop_file(path="/simon/note.txt"):
    return {".tag": "file", "name": "note.txt", "path_lower": path, "size": 5, "rev": "abc123"}


def box_item(identifier="1", kind="file", parent="10"):
    return {
        "id": identifier,
        "type": kind,
        "name": "notes",
        "size": 5,
        "etag": "etag",
        "parent": {"id": parent, "type": "folder"},
        "file_version": {"id": "123"},
        "path_collection": {"entries": [{"id": parent, "type": "folder"}]},
    }


def graph_item(identifier="file", parent="root", folder=False):
    return {
        "id": identifier,
        "name": identifier,
        "size": 5,
        "eTag": '"etag"',
        "parentReference": {"id": parent, "driveId": "drive"},
        ("folder" if folder else "file"): {},
    }


def test_all_cloud_factories_are_disabled_and_do_not_claim_unimplemented_writes():
    definitions = (
        *dropbox_tool_definitions(),
        *box_tool_definitions(),
        *onedrive_tool_definitions(),
    )
    assert len(definitions) == 11
    assert all(
        not item.enabled and not item.configured and item.settings["network"]
        for item in definitions
    )
    assert {item.id for item in definitions if item.side_effect} == {"dropbox.upload"}


@pytest.mark.parametrize("provider", ["dropbox", "box", "onedrive"])
@pytest.mark.parametrize("change", ["actor", "workspace", "tool", "scope", "endpoint"])
def test_cloud_scope_and_fixed_endpoint_validation_precedes_network(provider, change):
    tool, context, handler, calls = setup(provider)
    if change == "endpoint":
        tool = tool.model_copy(update={"endpoint": "https://attacker.example"})
    else:
        key, value = {
            "actor": ("actor_id", uuid4()),
            "workspace": ("workspace_id", uuid4()),
            "tool": ("allowed_tool_ids", frozenset()),
            "scope": ("scopes", frozenset()),
        }[change]
        context = context.model_copy(update={key: value})
    with pytest.raises((ToolCatalogError, AuthorizationError)):
        handler(tool, {}, context)
    assert not calls


def test_dropbox_list_replays_only_server_scoped_cursors_for_numeric_pages():
    def response(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/list_folder"):
            assert body["path"] == "/Simon" and body["recursive"] is False
            return httpx.Response(
                200, json={"entries": [drop_file()], "has_more": True, "cursor": "scoped-cursor"}
            )
        assert body == {"cursor": "scoped-cursor"}
        return httpx.Response(
            200,
            json={"entries": [drop_file("/simon/second.txt")], "has_more": False, "cursor": "end"},
        )

    tool, context, handler, calls = setup(respond=response)
    result = handler(tool, {"page": 2}, context)
    assert result["entries"][0]["path"] == "second.txt" and result["next_page"] is None
    assert len(calls) == 2
    with pytest.raises(ToolCatalogError):
        handler(tool, {"cursor": "different-folder"}, context)


def test_dropbox_scoped_search_and_metadata_drop_unrelated_provider_fields():
    def response(request):
        body = json.loads(request.content)
        assert body["options"]["path"] == "/Simon/sub" and body["options"]["filename_only"]
        return httpx.Response(
            200,
            json={
                "matches": [
                    {"metadata": {".tag": "metadata", "metadata": drop_file("/simon/sub/note.txt")}}
                ],
                "has_more": False,
            },
        )

    tool, context, handler, _ = setup(operation="search", respond=response)
    assert handler(tool, {"path": "sub", "query": "note"}, context)["entries"][0]["rev"] == "abc123"


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/private",
        "id:outside",
        "rev:123",
        "ns:123/file",
        "a\\b",
        "%2e%2e/private",
        "a//b",
    ],
)
def test_dropbox_model_paths_cannot_override_configured_root(path):
    tool, context, handler, calls = setup(operation="read")
    with pytest.raises(ToolCatalogError):
        handler(tool, {"path": path}, context)
    assert not calls


def test_dropbox_download_requires_atomic_metadata_to_match_scoped_path():
    download_metadata = drop_file()
    download_metadata.pop(".tag")  # Content endpoints return an untagged FileMetadata struct.
    tool, context, handler, calls = setup(
        operation="read",
        respond=lambda _: httpx.Response(
            200,
            content=b"hello",
            headers={"Dropbox-API-Result": json.dumps(download_metadata)},
        ),
    )
    result = handler(tool, {"path": "note.txt", "encoding": "base64"}, context)
    assert base64.b64decode(result["content"]) == b"hello"
    assert calls[0].url.host == "content.dropboxapi.com"
    assert json.loads(calls[0].headers["dropbox-api-arg"]) == {"path": "/Simon/note.txt"}
    for path in ["/simon-other/note.txt", "/private/note.txt", "/simon/different.txt"]:
        tool, context, handler, _ = setup(
            operation="read",
            respond=lambda _, p=path: httpx.Response(
                200,
                content=b"secret",
                headers={"Dropbox-API-Result": json.dumps(drop_file(p))},
            ),
        )
        with pytest.raises(ToolExecutionError):
            handler(tool, {"path": "note.txt"}, context)


def test_dropbox_upload_never_renames_or_overwrites_without_matching_revision():
    upload_metadata = drop_file()
    upload_metadata.pop(".tag")
    tool, context, handler, calls = setup(
        operation="upload",
        respond=lambda _: httpx.Response(
            200,
            json=upload_metadata,
        ),
    )
    arguments = {"path": "note.txt", "content": "hello", "expected_revision": ""}
    with pytest.raises(AuthorizationError):
        handler(tool, arguments, context.model_copy(update={"authorized_action": "read"}))
    assert handler(tool, arguments, context)["written"]
    first = json.loads(calls[0].headers["dropbox-api-arg"])
    assert first["mode"] == "add" and first["strict_conflict"] and not first["autorename"]
    handler(tool, arguments | {"expected_revision": "abc123"}, context)
    assert json.loads(calls[1].headers["dropbox-api-arg"])["mode"] == {
        ".tag": "update",
        "update": "abc123",
    }
    assert calls[1].content == b"hello"


@pytest.mark.parametrize("status,unknown", [(409, False), (503, True)])
def test_dropbox_failed_upload_has_known_conflict_or_unknown_server_outcome(status, unknown):
    tool, context, handler, calls = setup(
        operation="upload",
        respond=lambda _: httpx.Response(
            status,
            text=SECRET,
        ),
    )
    with pytest.raises(ToolExecutionError) as error:
        handler(tool, {"path": "note", "content": "hello", "expected_revision": "old"}, context)
    assert error.value.unknown is unknown and SECRET not in str(error.value) and len(calls) == 1


def test_box_ancestry_is_checked_before_content_is_downloaded():
    tool, context, handler, calls = setup(
        "box",
        "read",
        lambda _: httpx.Response(
            200,
            json=box_item(parent="999"),
        ),
    )
    with pytest.raises(AuthorizationError):
        handler(tool, {"item_id": "1"}, context)
    assert len(calls) == 1 and calls[0].url.path == "/2.0/files/1"


def test_box_download_drops_bearer_and_rechecks_ancestry_and_version():
    def response(request):
        if request.url.host == "dl.boxcloud.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"hello")
        assert request.headers["authorization"] == "Bearer " + SECRET
        if request.url.path.endswith("/content"):
            assert request.url.params["version"] == "123"
            return httpx.Response(302, headers={"Location": "https://dl.boxcloud.com/d/signed"})
        return httpx.Response(200, json=box_item())

    tool, context, handler, calls = setup("box", "read", response)
    result = handler(tool, {"item_id": "1"}, context)
    assert result["content"] == "hello" and len(calls) == 4
    assert "signed" not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://dl.boxcloud.com.evil.example/file",
        "http://dl.boxcloud.com/x",
        "https://evil.example/file",
        "https://user:password@dl.boxcloud.com/x",
        "https://dl.boxcloud.com:444/file",
    ],
)
def test_box_never_follows_unapproved_download_redirect(url):
    tool, context, handler, calls = setup(
        "box",
        "read",
        lambda request: (
            httpx.Response(
                302,
                headers={"Location": url},
            )
            if request.url.path.endswith("/content")
            else httpx.Response(200, json=box_item())
        ),
    )
    with pytest.raises(ToolExecutionError, match="not explicitly permitted"):
        handler(tool, {"item_id": "1"}, context)
    assert len(calls) == 2


def test_box_folder_listing_checks_returned_parent_and_scope_again():
    def response(request):
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"entries": [box_item()], "total_count": 1})
        return httpx.Response(200, json=box_item("10", "folder", "0"))

    tool, context, handler, calls = setup("box", "list", response)
    assert handler(tool, {}, context)["entries"][0]["id"] == "1" and len(calls) == 3


def test_onedrive_parent_chain_and_explicit_signed_host_download():
    def response(request):
        if request.url.host == "tenant.sharepoint.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"hello")
        if request.url.path.endswith("/content"):
            return httpx.Response(
                302, headers={"location": "https://tenant.sharepoint.com/download?s=secret"}
            )
        if request.url.path.endswith("/root"):
            return httpx.Response(200, json=graph_item("root", "drive-root", True))
        return httpx.Response(200, json=graph_item())

    tool, context, handler, calls = setup("onedrive", "read", response)
    result = handler(tool, {"path": "notes.txt"}, context)
    assert result["content"] == "hello" and len(calls) == 6
    assert all(
        "authorization" not in call.headers
        for call in calls
        if call.url.host != "graph.microsoft.com"
    )
    assert "download" not in json.dumps(result)


@pytest.mark.parametrize("change", ["remote", "drive", "cycle"])
def test_onedrive_rejects_shortcuts_cross_drive_and_cyclic_ancestry(change):
    item = graph_item(parent="file" if change == "cycle" else "root")
    if change == "remote":
        item["remoteItem"] = {"id": "elsewhere"}
    if change == "drive":
        item["parentReference"]["driveId"] = "another-drive"
    tool, context, handler, calls = setup(
        "onedrive", "read", lambda _: httpx.Response(200, json=item)
    )
    with pytest.raises(AuthorizationError):
        handler(tool, {"path": "notes.txt"}, context)
    assert all(not call.url.path.endswith("/content") for call in calls)


def test_onedrive_list_ignores_model_ids_and_rejects_external_continuation_urls():
    def response(request):
        if request.url.path.endswith("/children"):
            return httpx.Response(
                200, json={"value": [graph_item()], "@odata.nextLink": "https://evil.example/steal"}
            )
        return httpx.Response(200, json=graph_item("root", "drive-root", True))

    tool, context, handler, calls = setup("onedrive", "list", response)
    assert handler(tool, {}, context)["entries"][0]["id"] == "file"
    with pytest.raises(ToolExecutionError, match="page link escaped"):
        handler(tool, {"page": 2}, context)
    assert all(call.url.host == "graph.microsoft.com" for call in calls)


def test_cloud_preflight_honestly_reports_missing_download_host_and_credentials():
    tool, context, _, _ = setup("onedrive", "read")
    actor = ActorContext(
        actor_id=context.actor_id,
        workspace_id=context.workspace_id,
        channel=Channel.WORKER,
        scopes=context.scopes,
    )
    assert cloud_storage_tool_status(tool, actor, {})[0] == "unconfigured"
    assert cloud_storage_tool_status(tool, actor, {"SIMON_ONEDRIVE_TOKEN": SECRET}) == (
        "configured",
        (),
    )
    tool = tool.model_copy(update={"settings": tool.settings | {"download_hosts": []}})
    assert (
        cloud_storage_tool_status(tool, actor, {"SIMON_ONEDRIVE_TOKEN": SECRET})[0]
        == "unconfigured"
    )


def test_box_moved_file_is_not_returned_after_download():
    metadata_calls = 0

    def response(request):
        nonlocal metadata_calls
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"hello")
        metadata_calls += 1
        return httpx.Response(200, json=box_item(parent="10" if metadata_calls == 1 else "999"))

    tool, context, handler, calls = setup("box", "read", response)
    with pytest.raises(AuthorizationError, match="outside the configured folder"):
        handler(tool, {"item_id": "1"}, context)
    assert len(calls) == 3


def test_onedrive_changed_etag_is_not_returned_after_download():
    metadata_calls = 0

    def response(request):
        nonlocal metadata_calls
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"hello")
        if request.url.path.endswith("/root"):
            return httpx.Response(200, json=graph_item("root", "drive-root", True))
        metadata_calls += 1
        item = graph_item()
        item["eTag"] = '"first"' if metadata_calls == 1 else '"changed"'
        return httpx.Response(200, json=item)

    tool, context, handler, _ = setup("onedrive", "read", response)
    with pytest.raises(ToolExecutionError, match="changed while reading"):
        handler(tool, {"path": "notes.txt"}, context)


def test_dropbox_oversized_upload_rejected_before_network_and_bad_receipt_is_unknown():
    tool, context, handler, calls = setup(
        "dropbox", "upload", lambda _: httpx.Response(200, json=drop_file("/simon/%invalid"))
    )
    with pytest.raises(ToolCatalogError, match="256 KiB"):
        handler(
            tool, {"path": "note.txt", "content": "x" * 262145, "expected_revision": ""}, context
        )
    assert not calls
    with pytest.raises(ToolExecutionError) as failure:
        handler(tool, {"path": "note.txt", "content": "hello", "expected_revision": ""}, context)
    assert failure.value.unknown and len(calls) == 1
