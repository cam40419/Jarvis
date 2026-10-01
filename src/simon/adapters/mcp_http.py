"""Bounded, invocation-scoped MCP Streamable HTTP client (2025-11-25).

Only a configured tool is called. Server instructions, sampling, elicitation,
resource links and reconnect/replay requests cannot expand the client's access.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

import httpx

from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.domain.tool_catalog import ToolExecutionError as ExecutionError

PROTOCOL_VERSION = "2025-11-25"


class _ProtocolError(Exception):
    pass


class _Rejected(Exception):
    """A definitive protocol or HTTP rejection before the tool could run."""


class MCPHttpTransport:
    def __init__(
        self, *, timeout_seconds: float = 30, max_response_bytes: int = 2_000_000,
        transport: httpx.BaseTransport | None = None, environ: Mapping[str, str] | None = None,
        before_call: Callable[[], Any] | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 300 or not 0 < max_response_bytes <= 10_000_000:
            raise ValueError("MCP transport limits are outside their supported range")
        self.timeout_seconds, self.max_response_bytes = timeout_seconds, max_response_bytes
        self._transport = transport
        self._environ = os.environ if environ is None else environ
        self.before_call = before_call

    def __call__(
        self, definition: ToolDefinition, arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if not definition.endpoint:
            raise ToolCatalogError("MCP tool requires a configured Streamable HTTP endpoint")
        name = definition.settings.get("tool_name")
        if not isinstance(name, str) or not 1 <= len(name) <= 200 or any(
            ord(char) < 33 or ord(char) > 126 for char in name
        ):
            raise ToolCatalogError("MCP tool requires a fixed tool_name")
        if definition.settings.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise ToolCatalogError("MCP tool requires supported protocol version 2025-11-25")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
            "X-Actor-ID": str(context.actor_id), "X-Household-ID": str(context.household_id),
            "X-Run-ID": str(context.run_id), "X-Agent-ID": context.agent_id,
            "X-Invocation-ID": str(context.invocation_id),
        }
        if definition.credential_env:
            secret = self._environ.get(definition.credential_env, "").strip()
            if not secret:
                raise ToolCatalogError("MCP credential environment variable is unavailable")
            headers["Authorization"] = f"Bearer {secret}"
        deadline = monotonic() + self.timeout_seconds
        dispatched = False
        session_id: str | None = None
        with httpx.Client(
            transport=self._transport, timeout=self.timeout_seconds,
            trust_env=False, follow_redirects=False,
        ) as client:
            try:
                initialized, session_id = self._request(
                    client, definition.endpoint, headers, {
                        "jsonrpc": "2.0", "id": f"{context.invocation_id}:initialize",
                        "method": "initialize", "params": {
                            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                            "clientInfo": {"name": "simon-agent", "version": "1.0.0"},
                        },
                    }, deadline,
                )
                headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
                if session_id is not None:
                    if not 1 <= len(session_id) <= 1024 or any(
                        not 0x21 <= ord(char) <= 0x7E for char in session_id
                    ):
                        session_id = None
                        raise _ProtocolError("Invalid MCP session identifier")
                    headers["MCP-Session-Id"] = session_id
                if initialized.get("protocolVersion") != PROTOCOL_VERSION:
                    raise _ProtocolError("Server negotiated an unsupported protocol version")
                capabilities = initialized.get("capabilities")
                if not isinstance(capabilities, dict) or not isinstance(
                    capabilities.get("tools"), dict,
                ):
                    raise _ProtocolError("Server did not advertise tools")
                self._notify(client, definition.endpoint, headers, {
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                }, deadline)
                if self.before_call is not None:
                    self.before_call()
                self._remaining(deadline)
                dispatched = True
                result, _ = self._request(client, definition.endpoint, headers, {
                    "jsonrpc": "2.0", "id": str(context.invocation_id), "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }, deadline)
                if result.get("isError", False) is not False:
                    raise ExecutionError(
                        "MCP tool reported an error", unknown=definition.side_effect,
                    )
                if not isinstance(result.get("content"), list) and not isinstance(
                    result.get("structuredContent"), dict,
                ):
                    raise _ProtocolError("MCP tool response has no supported result content")
                return result
            except _Rejected:
                raise ExecutionError("MCP server rejected the request", unknown=False) from None
            except (httpx.HTTPError, _ProtocolError, ValueError, RecursionError):
                raise ExecutionError(
                    "MCP session failed or returned an invalid response",
                    unknown=dispatched and definition.side_effect,
                ) from None
            finally:
                if session_id is not None and "MCP-Session-Id" in headers:
                    # DELETE is session cleanup, never a replay of tools/call. Failure
                    # cannot erase a successful operation receipt. 405 is permitted.
                    try:
                        with client.stream(
                            "DELETE", definition.endpoint, headers=headers,
                            timeout=min(2.0, max(0.1, deadline - monotonic())),
                        ):
                            pass
                    except (httpx.HTTPError, ValueError):
                        pass

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _ProtocolError("MCP invocation deadline exceeded")
        return remaining

    @staticmethod
    def _http_status(response: httpx.Response) -> None:
        if response.status_code in {400, 401, 403, 404, 405, 422}:
            raise _Rejected
        if not 200 <= response.status_code < 300:
            raise _ProtocolError("MCP endpoint rejected request")

    def _notify(
        self, client: httpx.Client, endpoint: str, headers: dict[str, str],
        message: dict[str, Any], deadline: float,
    ) -> None:
        with client.stream(
            "POST", endpoint, headers=headers, json=message, timeout=self._remaining(deadline),
        ) as response:
            self._http_status(response)
            if response.status_code != 202:
                raise _ProtocolError("MCP notification was not accepted")

    def _request(
        self, client: httpx.Client, endpoint: str, headers: dict[str, str],
        message: dict[str, Any], deadline: float,
    ) -> tuple[dict[str, Any], str | None]:
        with client.stream(
            "POST", endpoint, headers=headers, json=message, timeout=self._remaining(deadline),
        ) as response:
            self._http_status(response)
            content_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
            if content_type == "text/event-stream":
                result = self._sse(response, message["id"], deadline)
            elif content_type == "application/json":
                data = bytearray()
                for chunk in response.iter_bytes():
                    self._remaining(deadline)
                    data.extend(chunk)
                    if len(data) > self.max_response_bytes:
                        raise _ProtocolError("MCP response exceeds the byte limit")
                parsed = self._message(bytes(data), message["id"])
                if parsed is None:
                    raise _ProtocolError("MCP request returned a notification")
                result = parsed
            else:
                raise _ProtocolError("Unsupported MCP response content type")
            return result, response.headers.get("MCP-Session-Id")

    @staticmethod
    def _message(data: bytes, request_id: str) -> dict[str, Any] | None:
        message = json.loads(data.decode("utf-8"))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise _ProtocolError("Invalid JSON-RPC message")
        if "method" in message:
            if "id" in message:
                # Client advertises no roots, sampling, elicitation or task capabilities.
                raise _ProtocolError("Server initiated an unsupported client request")
            if not isinstance(message["method"], str):
                raise _ProtocolError("Invalid server notification")
            return None
        if message.get("id") != request_id or ("result" in message) == ("error" in message):
            raise _ProtocolError("MCP response does not match this invocation")
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict) and error.get("code") in {-32600, -32601, -32602}:
                raise _Rejected
            raise _ProtocolError("MCP server reported an error")
        if not isinstance(message["result"], dict):
            raise _ProtocolError("MCP result must be an object")
        return message["result"]

    def _sse(
        self, response: httpx.Response, request_id: str, deadline: float,
    ) -> dict[str, Any]:
        pending = bytearray()
        lines: list[bytes] = []
        received = 0
        events = 0
        skip_lf = False
        for chunk in response.iter_bytes():
            self._remaining(deadline)
            received += len(chunk)
            if received > self.max_response_bytes:
                raise _ProtocolError("MCP stream exceeds the byte limit")
            for byte in chunk:
                if skip_lf:
                    skip_lf = False
                    if byte == 10:
                        continue
                if byte not in {10, 13}:
                    pending.append(byte)
                    continue
                skip_lf = byte == 13
                line = bytes(pending)
                pending.clear()
                if line.startswith(b"data:"):
                    value = line[5:]
                    lines.append(value[1:] if value.startswith(b" ") else value)
                elif line == b"" and lines:
                    data = b"\n".join(lines)
                    lines.clear()
                    events += 1
                    if events > 128:
                        raise _ProtocolError("MCP stream exceeds the event limit")
                    if data.strip():
                        result = self._message(data, request_id)
                        if result is not None:
                            return result
        # Interrupted/unfinished SSE requests are deliberately not resumed or replayed.
        raise _ProtocolError("MCP stream ended before its response")
