import json
from uuid import uuid4

import httpx
import pytest

from simon.adapters.mcp_http import PROTOCOL_VERSION, MCPHttpTransport
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import AuthorizationError
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.domain.tool_catalog import ToolExecutionError as ExecutionError


def definition(*, write=False, **changes):
    values = {
        "id": "storage.search",
        "description": "Search a configured storage account",
        "transport": "mcp",
        "endpoint": "https://mcp.example.test/mcp",
        "configured": True,
        "required_scopes": frozenset({"jobs:write" if write else "jobs:read"}),
        "credential_env": "MCP_KEY",
        "side_effect": write,
        "action_policy": "write" if write else "read",
        "settings": {"tool_name": "search_files", "protocol_version": PROTOCOL_VERSION},
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    values.update(changes)
    return ToolDefinition(**values)


def context(*, write=False):
    return ToolExecutionContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        run_id=uuid4(),
        agent_id="worker",
        scopes=frozenset({"jobs:read", "jobs:write"}),
        allowed_tool_ids=frozenset({"storage.search"}),
        authorized_action="write" if write else "read",
    )


def json_response(body, result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def server(operate=None, *, initialize=None, notify=None, cleanup=None, session="session-123"):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "DELETE":
            return cleanup(request) if cleanup else httpx.Response(405)
        assert request.url == "https://mcp.example.test/mcp"
        body = json.loads(request.content)
        if body["method"] == "initialize":
            assert "MCP-Session-Id" not in request.headers
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "instructions": "Untrusted server instructions",
            }
            response = initialize(body) if initialize else json_response(body, result)
            if session is not None:
                response.headers["MCP-Session-Id"] = session
            return response
        assert request.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
        if session is not None:
            assert request.headers["MCP-Session-Id"] == session
        if body["method"] == "notifications/initialized":
            return notify(body) if notify else httpx.Response(202)
        assert body["method"] == "tools/call"
        return (
            operate(request, body)
            if operate
            else json_response(
                body,
                {
                    "content": [{"type": "text", "text": "A file"}],
                    "isError": False,
                },
            )
        )

    return requests, httpx.MockTransport(respond)


def client(transport, **kwargs):
    return MCPHttpTransport(transport=transport, environ={"MCP_KEY": "not-logged"}, **kwargs)


def test_full_session_exact_tool_and_provenance_then_cleanup():
    seen, backend = server()
    tool, owner = definition(), context()
    registry = TransportRegistry()
    registry.register("mcp", client(backend))
    result = registry.execute(tool, {"query": "notes"}, owner)
    assert result.output["content"][0]["text"] == "A file"
    assert result.actor_id == owner.actor_id and result.invocation_id == owner.invocation_id
    assert len(seen) == 4
    initialize = json.loads(seen[0].content)
    assert initialize["params"]["capabilities"] == {}
    call = json.loads(seen[2].content)
    assert call["id"] == str(owner.invocation_id)
    assert call["params"] == {"name": "search_files", "arguments": {"query": "notes"}}
    for request in seen:
        assert request.headers["Authorization"] == "Bearer not-logged"
        assert request.headers["X-Actor-ID"] == str(owner.actor_id)
        assert request.headers["X-Invocation-ID"] == str(owner.invocation_id)
        assert "application/json" in request.headers["Accept"]
        assert "text/event-stream" in request.headers["Accept"]
    assert seen[-1].method == "DELETE"


def test_stateless_server_has_no_delete_and_only_one_call():
    seen, backend = server(session=None)
    client(backend)(definition(), {}, context())
    assert len(seen) == 3 and all(request.method == "POST" for request in seen)


class Chunked(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_sse_multiline_utf8_split_chunks_notifications_and_early_result(newline):
    def operate(request, body):
        note = json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
        )
        result = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": "Résumé"}],
                    "structuredContent": {"count": 1},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        content = (
            "id: first"
            + newline
            + "data:"
            + newline * 2
            + ": heartbeat"
            + newline
            + "data: "
            + note
            + newline * 2
            + newline.join("data: " + line for line in result.splitlines())
            + newline * 2
        ).encode()
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=Chunked([bytes([byte]) for byte in content]),
        )

    _, backend = server(operate)
    result = client(backend)(definition(), {}, context())
    assert result["structuredContent"]["count"] == 1
    assert result["content"][0]["text"] == "Résumé"


@pytest.mark.parametrize("status,unknown", [(401, False), (404, False), (500, True), (302, True)])
def test_http_failures_never_retry_writes_and_cleanup(status, unknown):
    seen, backend = server(
        lambda request, body: httpx.Response(
            status,
            headers={"Location": "https://other.example.test/steal"},
        )
    )
    with pytest.raises(ExecutionError) as raised:
        client(backend)(definition(write=True), {}, context(write=True))
    assert raised.value.unknown is unknown
    assert len(seen) == 4 and seen[-1].method == "DELETE"
    assert all(request.url.host == "mcp.example.test" for request in seen)


@pytest.mark.parametrize("code,unknown", [(-32601, False), (-32602, False), (-32603, True)])
def test_rpc_errors_redact_server_data_and_classify_unknown(code, unknown):
    seen, backend = server(
        lambda request, body: httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": code, "message": "secret credentials", "data": "secret"},
            },
        )
    )
    with pytest.raises(ExecutionError) as raised:
        client(backend)(definition(write=True), {}, context(write=True))
    assert raised.value.unknown is unknown and "secret" not in str(raised.value)
    assert len(seen) == 4


@pytest.mark.parametrize("write", [False, True])
def test_network_loss_after_call_is_unknown_only_for_side_effects(write):
    def lose(request, body):
        raise httpx.ReadTimeout("Bearer must-not-leak", request=request)

    seen, backend = server(lose)
    with pytest.raises(ExecutionError) as raised:
        client(backend)(definition(write=write), {}, context(write=write))
    assert raised.value.unknown is write and "Bearer" not in str(raised.value)
    assert len(seen) == 4


@pytest.mark.parametrize(
    "result",
    [
        {"protocolVersion": "2026-07-28", "capabilities": {"tools": {}}},
        {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
    ],
)
def test_bad_handshake_never_dispatches_tool(result):
    seen, backend = server(initialize=lambda body: json_response(body, result))
    with pytest.raises(ExecutionError) as raised:
        client(backend)(definition(write=True), {}, context(write=True))
    assert not raised.value.unknown
    assert len(seen) == 2 and seen[-1].method == "DELETE"


def test_notify_failure_never_dispatches_tool():
    seen, backend = server(notify=lambda body: httpx.Response(500))
    with pytest.raises(ExecutionError) as raised:
        client(backend)(definition(write=True), {}, context(write=True))
    assert not raised.value.unknown and len(seen) == 3
    assert seen[-1].method == "DELETE"


def test_cancel_after_handshake_prevents_call_and_closes_session():
    seen, backend = server()

    def cancelled():
        raise AuthorizationError("Assignment cancelled")

    with pytest.raises(AuthorizationError, match="cancelled"):
        client(backend, before_call=cancelled)(definition(write=True), {}, context(write=True))
    assert len(seen) == 3 and seen[-1].method == "DELETE"


@pytest.mark.parametrize(
    "payload",
    [
        lambda identifier: {"jsonrpc": "2.0", "id": "wrong", "result": {"content": []}},
        lambda identifier: {"jsonrpc": "2.0", "id": identifier, "result": []},
        lambda identifier: {"jsonrpc": "2.0", "id": identifier, "result": {}},
        lambda identifier: {"jsonrpc": "2.0", "id": 17, "method": "sampling/createMessage"},
    ],
)
def test_bad_responses_and_server_requests_cannot_dispatch_other_actions(payload):
    seen, backend = server(lambda request, body: httpx.Response(200, json=payload(body["id"])))
    with pytest.raises(ExecutionError) as raised:
        client(backend)(definition(write=True), {}, context(write=True))
    assert raised.value.unknown and len(seen) == 4


@pytest.mark.parametrize("kind", ["oversized_json", "oversized_sse", "incomplete_sse", "is_error"])
def test_bounded_responses_preserve_unknown_write_outcome(kind):
    def operate(request, body):
        if kind == "oversized_json":
            return json_response(body, {"content": [{"text": "x" * 5000}]})
        if kind == "is_error":
            return json_response(body, {"content": [], "isError": True})
        text = "data: " + ("x" * 5000 if kind == "oversized_sse" else "{}")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=text)

    seen, backend = server(operate)
    with pytest.raises(ExecutionError) as raised:
        client(backend, max_response_bytes=1000)(definition(write=True), {}, context(write=True))
    assert raised.value.unknown and len(seen) == 4


def test_cleanup_failure_does_not_erase_success():
    def cleanup(request):
        raise httpx.ReadTimeout("cleanup failed", request=request)

    _, backend = server(cleanup=cleanup)
    assert client(backend)(definition(write=True), {}, context(write=True))["isError"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": None},
        {"settings": {}},
        {"settings": {"tool_name": "bad\nname"}},
        {"settings": {"tool_name": "search", "protocol_version": "unsupported"}},
        {"credential_env": "UNSET_KEY"},
    ],
)
def test_invalid_configuration_rejected_before_network(changes):
    seen, backend = server()
    with pytest.raises(ToolCatalogError):
        client(backend)(definition(**changes), {}, context())
    assert seen == []
