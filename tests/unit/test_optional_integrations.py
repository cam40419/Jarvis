from __future__ import annotations

import base64
import json
from uuid import uuid4

import httpx
import pytest

from simon.adapters.github_tools import GitHubTransport, github_tool_definitions
from simon.adapters.optional_http import optional_tool_status
from simon.adapters.tool_transports import TransportRegistry
from simon.adapters.webdav_tools import WebDAVTransport, webdav_tool_definitions
from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError

SECRET = "synthetic-service-credential"


def configured(kind="github", operation="repository", handler=None, **changes):
    actor, workspace = uuid4(), uuid4()
    options = {"enabled": True, "workspace_id": workspace, "actor_ids": [actor]}
    if kind == "github":
        tools = github_tool_definitions(**options, repositories=["example/allowed"])
        cls, environment = GitHubTransport, {"SIMON_GITHUB_TOKEN": SECRET}
    else:
        tools = webdav_tool_definitions(
            **options,
            endpoint="https://files.example/remote.php/dav/files/service/Simon",
            username="service",
        )
        cls, environment = WebDAVTransport, {"SIMON_WEBDAV_PASSWORD": SECRET}
    definition = next(tool for tool in tools if tool.id == kind + "." + operation)
    context = ToolExecutionContext(
        actor_id=actor,
        household_id=workspace,
        run_id=uuid4(),
        agent_id="assistant",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=definition.required_scopes,
        authorized_action=definition.action_policy,
    )
    requests = []

    def record(request):
        requests.append(request)
        return (
            handler(request)
            if handler
            else httpx.Response(200, json={"full_name": "example/allowed"})
        )

    transport = cls(transport=httpx.MockTransport(record), environ=environment, **changes)
    return definition, context, transport, requests


def test_factories_are_disabled_and_require_explicit_service_account_grants():
    for definition in (*github_tool_definitions(), *webdav_tool_definitions()):
        assert not definition.enabled and not definition.configured
        assert definition.required_scopes in [frozenset({"jobs:read"}), frozenset({"jobs:write"})]


def test_github_repository_and_list_page_use_fixed_endpoints_and_project_response():
    tool, context, handler, requests = configured(
        handler=lambda _: httpx.Response(
            200,
            json={
                "full_name": "example/allowed",
                "description": "Token: " + SECRET,
                "private": True,
                "unrelated_secret": SECRET,
            },
        )
    )
    registry = TransportRegistry()
    registry.register("github", handler)
    result = registry.execute(tool, {"repository": "example/allowed"}, context).output
    assert result == {
        "full_name": "example/allowed",
        "private": True,
        "description": "Token: [redacted]",
    }
    assert str(requests[0].url) == "https://api.github.com/repos/example/allowed"
    assert requests[0].headers["authorization"] == "Bearer " + SECRET
    assert requests[0].headers["x-github-api-version"] == "2026-03-10"
    tool, context, handler, requests = configured(
        "github",
        "issues",
        lambda _: httpx.Response(
            200,
            json=[{"number": 1, "title": "Pull", "pull_request": {}}],
            headers={"link": '<https://unrelated.example/steal>; rel="next"'},
        ),
    )
    result = handler(tool, {"repository": "example/allowed", "per_page": 10}, context)
    assert result["next_page"] == 2 and result["items"][0]["kind"] == "pull_request"
    assert requests[0].url.params["per_page"] == "10" and len(requests) == 1


@pytest.mark.parametrize("kind,operation", [("github", "repository"), ("webdav", "list")])
@pytest.mark.parametrize("change", ["actor", "workspace", "tool", "scope", "grant"])
def test_cross_actor_workspace_and_assignment_access_fail_before_http(kind, operation, change):
    tool, context, handler, requests = configured(kind, operation)
    if change == "actor":
        context = context.model_copy(update={"actor_id": uuid4()})
    elif change == "workspace":
        context = context.model_copy(update={"household_id": uuid4()})
    elif change == "tool":
        context = context.model_copy(update={"allowed_tool_ids": frozenset()})
    elif change == "scope":
        context = context.model_copy(update={"scopes": frozenset()})
    else:
        tool = tool.model_copy(update={"settings": tool.settings | {"actor_ids": []}})
    with pytest.raises((AuthorizationError, ToolCatalogError)):
        handler(tool, {"repository": "example/allowed"} if kind == "github" else {}, context)
    assert not requests


def test_repository_allowlist_and_canonical_schema_cannot_be_widened():
    tool, context, handler, requests = configured()
    with pytest.raises(AuthorizationError):
        handler(tool, {"repository": "example/private"}, context)
    tool = tool.model_copy(update={"input_schema": {"type": "object"}})
    with pytest.raises(ToolCatalogError):
        handler(tool, {"repository": "example/allowed", "url": "https://other.example"}, context)
    assert not requests


def test_github_file_read_preserves_content_sha_and_bounds_decoded_bytes():
    content = base64.b64encode(b"# Report\n").decode()
    tool, context, handler, requests = configured(
        "github",
        "file_read",
        lambda _: httpx.Response(
            200,
            json={"type": "file", "encoding": "base64", "content": content, "sha": "abc"},
        ),
    )
    result = handler(
        tool, {"repository": "example/allowed", "path": "docs/a b.md", "ref": "main"}, context
    )
    assert result["text"] == "# Report\n" and result["sha"] == "abc"
    assert requests[0].url.path == "/repos/example/allowed/contents/docs/a b.md"
    assert requests[0].url.params["ref"] == "main"
    for path in ("../secret", "%2e%2e/secret", "/other", "a\\b", "https://other/file"):
        with pytest.raises(ToolCatalogError):
            handler(tool, {"repository": "example/allowed", "path": path}, context)
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["issue_create", "pull_request_draft"])
def test_github_creates_issue_or_draft_only_with_write_grant(operation):
    tool, context, handler, requests = configured(
        "github",
        operation,
        lambda _: httpx.Response(
            201,
            json={
                "number": 7,
                "html_url": "https://github.com/example/allowed/issues/7",
                "title": "Review",
                "draft": True,
            },
        ),
    )
    arguments = {"repository": "example/allowed", "title": "Review", "body": "Exact\nnewlines"}
    if operation == "pull_request_draft":
        arguments.update({"head": "feature", "base": "main"})
    with pytest.raises(AuthorizationError):
        handler(tool, arguments, context.model_copy(update={"authorized_action": "read"}))
    result = handler(tool, arguments, context)
    assert result["number"] == 7 and len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["body"] == "Exact\nnewlines"
    if operation == "pull_request_draft":
        assert payload["draft"] is True and payload["maintainer_can_modify"] is False
        with pytest.raises(ToolCatalogError):
            handler(tool, arguments | {"head": "other:feature"}, context)


@pytest.mark.parametrize("status,unknown", [(302, True), (401, False), (422, False), (503, True)])
def test_write_http_failure_does_not_retry_or_echo_provider_response(status, unknown):
    tool, context, handler, requests = configured(
        "github",
        "issue_create",
        lambda _: httpx.Response(
            status,
            text=SECRET,
            headers={"location": "https://unrelated.example"},
        ),
    )
    with pytest.raises(ToolExecutionError) as error:
        handler(tool, {"repository": "example/allowed", "title": "Issue"}, context)
    assert error.value.unknown is unknown and SECRET not in str(error.value)
    assert len(requests) == 1


def dav_listing(href="/remote.php/dav/files/service/Simon/notes.txt"):
    return f"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
      <d:response><d:href>{href}</d:href><d:propstat>
        <d:prop><d:resourcetype/><d:getetag>"revision1"</d:getetag>
          <d:getcontentlength>5</d:getcontentlength><d:getcontenttype>text/plain</d:getcontenttype>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status>
      </d:propstat></d:response></d:multistatus>""".encode()


def test_webdav_listing_is_shallow_and_only_returns_relative_paths():
    tool, context, handler, requests = configured(
        "webdav",
        "list",
        lambda _: httpx.Response(
            207,
            content=dav_listing(),
        ),
    )
    result = handler(tool, {}, context)
    assert result["entries"][0] == {
        "name": "notes.txt",
        "path": "notes.txt",
        "kind": "file",
        "bytes": 5,
        "etag": '"revision1"',
        "media_type": "text/plain",
        "modified_at": "",
    }
    assert requests[0].method == "PROPFIND" and requests[0].headers["depth"] == "1"
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "path",
    [
        "../private",
        "%2e%2e/private",
        "/root",
        "a\\b",
        "https://evil.example",
        "a//b",
        "a/./b",
        "a\r\nb",
    ],
)
def test_webdav_path_traversal_is_rejected_before_network(path):
    tool, context, handler, requests = configured("webdav", "read")
    with pytest.raises(ToolCatalogError):
        handler(tool, {"path": path}, context)
    assert not requests


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/file",
        "/other/file",
        "/remote.php/dav/files/service/Simon/%2e%2e",
        "/remote.php/dav/files/service/Simon/sub/file",
        "/remote.php/dav/files/service/Simon/%252e%252e",
    ],
)
def test_webdav_listing_cannot_escape_root_or_follow_returned_hrefs(href):
    tool, context, handler, requests = configured(
        "webdav",
        "list",
        lambda _: httpx.Response(
            207,
            content=dav_listing(href),
        ),
    )
    with pytest.raises((ToolExecutionError, ToolCatalogError)):
        handler(tool, {}, context)
    assert len(requests) == 1


def test_webdav_rejects_xml_entities_and_redirects():
    entity = b'<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]><x>&ext;</x>'
    for response in [
        httpx.Response(207, content=entity),
        httpx.Response(301, headers={"location": "https://evil.example"}),
    ]:
        tool, context, handler, requests = configured("webdav", "list", lambda _, r=response: r)
        with pytest.raises(ToolExecutionError):
            handler(tool, {}, context)
        assert len(requests) == 1


def test_webdav_read_and_conditional_create_update_preserve_revision():
    tool, context, handler, requests = configured(
        "webdav",
        "read",
        lambda _: httpx.Response(
            200,
            content=b"hello",
            headers={"etag": '"v1"', "content-type": "text/plain"},
        ),
    )
    assert handler(tool, {"path": "notes.txt"}, context)["etag"] == '"v1"'
    assert handler(tool, {"path": "notes.txt", "encoding": "base64"}, context)["content"] == (
        base64.b64encode(b"hello").decode()
    )
    tool, context, handler, requests = configured(
        "webdav",
        "write",
        lambda _: httpx.Response(
            201,
            headers={"etag": '"v2"'},
        ),
    )
    result = handler(tool, {"path": "notes.txt", "content": "new", "expected_etag": ""}, context)
    assert result["written"] is True and requests[0].headers["if-none-match"] == "*"
    handler(tool, {"path": "notes.txt", "content": "next", "expected_etag": '"v2"'}, context)
    assert requests[1].headers["if-match"] == '"v2"' and "if-none-match" not in requests[1].headers
    assert requests[1].content == b"next"
    for revision in ["*", 'W/"weak"', '"one", "two"', "\r\ninjected", "bare"]:
        with pytest.raises(ToolCatalogError):
            handler(tool, {"path": "notes.txt", "content": "", "expected_etag": revision}, context)
    assert len(requests) == 2


def test_webdav_precondition_failure_is_known_and_timeout_is_unknown():
    def timeout(_):
        raise httpx.ReadTimeout(SECRET)

    for response, unknown in [(lambda _: httpx.Response(412), False), (timeout, True)]:
        tool, context, handler, requests = configured("webdav", "write", response)
        with pytest.raises(ToolExecutionError) as error:
            handler(tool, {"path": "notes", "content": "new", "expected_etag": '"old"'}, context)
        assert error.value.unknown is unknown and SECRET not in str(error.value)
        assert len(requests) == 1


def test_oversized_responses_are_bounded_and_reflected_credentials_redacted():
    tool, context, handler, _ = configured(
        "webdav",
        "read",
        lambda _: httpx.Response(
            200,
            content=SECRET.encode(),
        ),
    )
    result = handler(tool, {"path": "reflected", "encoding": "base64"}, context)
    assert base64.b64decode(result["content"]) == b"[redacted]" and result["redacted"]
    tool, context, handler, _ = configured(
        "webdav",
        "read",
        lambda _: httpx.Response(
            200,
            content=b"x" * 2048,
        ),
        max_response_bytes=1024,
    )
    with pytest.raises(ToolExecutionError, match="size limit"):
        handler(tool, {"path": "large"}, context)


def test_preflight_reports_missing_credentials_and_denied_tenant_without_network():
    tool, context, _, _ = configured()
    actor = ActorContext(
        actor_id=context.actor_id,
        household_id=context.household_id,
        channel=Channel.WORKER,
        scopes=context.scopes,
    )
    assert optional_tool_status(tool, actor, {})[0] == "unconfigured"
    assert optional_tool_status(tool, actor, {"SIMON_GITHUB_TOKEN": SECRET}) == ("configured", ())
    assert (
        optional_tool_status(
            tool, actor.model_copy(update={"actor_id": uuid4()}), {"SIMON_GITHUB_TOKEN": SECRET}
        )[0]
        == "permission_required"
    )
