from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from simon.adapters import _browser_runner as runner
from simon.adapters.browser_tools import (
    BROWSER_CAPABILITIES,
    BrowserToolTransport,
    browser_tool_definitions,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import AuthorizationError
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentRequest,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)
from simon.domain.models import utc_now
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)


class RecordingManager:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, ExecutionCommand, UUID, int]] = []
        self.fail = False

    def execute(
        self,
        lease_id: UUID,
        command: ExecutionCommand,
        *,
        attempt_id: UUID,
        fencing_token: int,
    ) -> ExecutionResult:
        self.calls.append((lease_id, command, attempt_id, fencing_token))
        if self.fail:
            raise ExecutionError("sensitive backend detail")
        return ExecutionResult(exit_code=0, stdout='{"title":"Example"}')


def setup(
    operation: str = "browser.read",
) -> tuple[
    RecordingManager,
    EnvironmentLease,
    ToolDefinition,
    ToolExecutionContext,
    BrowserToolTransport,
]:
    tool = next(item for item in browser_tool_definitions(enabled=True) if item.id == operation)
    tool.settings["allowed_origins"] = ["https://example.com"]
    definition = EnvironmentDefinition(
        id="browser",
        kind="docker",
        container_image="simon-browser:local",
        enabled=True,
        capabilities=BROWSER_CAPABILITIES,
        network="none" if operation == "browser.render_html" else "bridge",
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(),
        agent_id="browser",
        task_id=uuid4(),
        attempt_id=uuid4(),
        capabilities=BROWSER_CAPABILITIES,
    )
    lease = EnvironmentLease(
        id=uuid4(),
        plan=EnvironmentPlan(
            environment_id=definition.id,
            kind="docker",
            request=request,
            workspace_path=Path("unused"),
            network=definition.network,
            cpu_limit=2,
            memory_mb=2048,
            gpu_devices=(),
        ),
        definition=definition,
        fencing_token=8,
        status="active",
        resource_handle="owned",
        created_at=utc_now(),
        heartbeat_at=utc_now(),
    )
    context = ToolExecutionContext(
        actor_id=uuid4(),
        household_id=request.workspace_id,
        run_id=uuid4(),
        agent_id="browser",
        allowed_tool_ids=frozenset({tool.id}),
        scopes=tool.required_scopes,
        environment_capabilities=BROWSER_CAPABILITIES,
        authorized_action=tool.action_policy,
    )
    manager = RecordingManager()
    transport = BrowserToolTransport(
        manager,
        lease,
        actor_id=context.actor_id,
        run_id=context.run_id,
    )
    return manager, lease, tool, context, transport


def test_definitions_are_disabled_and_have_no_implicit_destinations() -> None:
    definitions = browser_tool_definitions()
    assert len(definitions) == 3
    assert all(not tool.enabled and not tool.configured for tool in definitions)
    assert all(tool.settings["allowed_origins"] == [] for tool in definitions)


def test_transport_uses_owned_lease_and_only_configured_origins() -> None:
    manager, lease, tool, context, transport = setup()
    registry = TransportRegistry()
    registry.register("browser", transport)
    result = registry.execute(tool, {"url": "https://example.com/article"}, context)
    assert result.output["exit_code"] == 0
    identifier, command, attempt, fence = manager.calls[0]
    assert (identifier, attempt, fence) == (lease.id, lease.plan.request.attempt_id, 8)
    assert command.argv[:3] == ("/usr/local/bin/python3", "-I", "-c")
    payload = json.loads(command.argv[4])
    assert payload["allowed_origins"] == ["https://example.com"]
    assert payload["arguments"]["max_text_chars"] == 8000


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example",
        "https://example.com.evil.test",
        "http://example.com",
        "https://user@example.com",
        "file:///etc/passwd",
        "https://example.com:8443",
        "https://example.com\\@evil.test",
    ],
)
def test_ungranted_or_invalid_url_never_reaches_runner(url: str) -> None:
    manager, _, tool, context, transport = setup()
    with pytest.raises(ToolCatalogError):
        transport(tool, {"url": url}, context)
    assert manager.calls == []


@pytest.mark.parametrize("field", ["actor_id", "household_id", "run_id", "agent_id"])
def test_cannot_cross_worker_assignment(field: str) -> None:
    manager, _, tool, context, transport = setup()
    value: Any = "other" if field == "agent_id" else uuid4()
    with pytest.raises(AuthorizationError):
        transport(tool, {"url": "https://example.com"}, context.model_copy(update={field: value}))
    assert manager.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"scopes": frozenset()},
        {"allowed_tool_ids": frozenset()},
        {"authorized_action": "read"},
    ],
)
def test_screenshots_need_write_authorization(change: dict[str, Any]) -> None:
    manager, _, tool, context, transport = setup("browser.screenshot")
    with pytest.raises(AuthorizationError):
        transport(
            tool,
            {"url": "https://example.com", "output": "shot.png"},
            context.model_copy(update=change),
        )
    assert manager.calls == []


def test_html_renderer_requires_offline_lease() -> None:
    manager, lease, tool, context, _ = setup("browser.render_html")
    lease = lease.model_copy(update={"plan": lease.plan.model_copy(update={"network": "bridge"})})
    transport = BrowserToolTransport(
        manager,
        lease,
        actor_id=context.actor_id,
        run_id=context.run_id,
    )
    with pytest.raises(ToolCatalogError, match="network capability"):
        transport(tool, {"input": "report.html", "output": "preview.png"}, context)
    assert manager.calls == []


@pytest.mark.parametrize(
    "value",
    [
        ["https://example.com/private"],
        ["https://*.example.com"],
        ["https://example.com?key=value"],
        "https://example.com",
    ],
)
def test_origin_grants_are_exact_bounded_origins(value: Any) -> None:
    with pytest.raises(ValueError):
        runner.allowed_origins(value)


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], ["93.184.216.34", "192.168.1.1"]],
)
def test_private_and_mixed_dns_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    addresses: list[str],
) -> None:
    monkeypatch.setattr(
        runner.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (address, 443)) for address in addresses],
    )
    with pytest.raises(ValueError, match="public Internet"):
        runner.public_address("example.com")


class FakeResponse:
    def __init__(
        self, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b"Example"
    ) -> None:
        self.status, self.headers, self.body = status, headers or {}, body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def fake_connections(
    monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runner, "public_address", lambda hostname: "93.184.216.34")

    class FakeConnection:
        def __init__(self, hostname: str, address: str, *, timeout: float) -> None:
            self.record = {"hostname": hostname, "address": address, "timeout": timeout}
            calls.append(self.record)

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            self.record.update(method=method, path=path, headers=headers)

        def getresponse(self) -> FakeResponse:
            return responses.pop(0)

        def close(self) -> None:
            self.record["closed"] = True

    monkeypatch.setattr(runner, "PinnedHTTPSConnection", FakeConnection)
    return calls


def test_each_redirect_is_checked_before_next_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_connections(monkeypatch, [FakeResponse(302, {"Location": "https://evil.test/"})])
    fetcher = runner.Fetcher(frozenset({"https://example.com"}), timeout_seconds=30)
    with pytest.raises(ValueError, match="outside"):
        fetcher.get("https://example.com/start")
    assert len(calls) == 1
    assert calls[0]["closed"]


def test_allowed_redirects_preserve_final_url_and_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_connections(
        monkeypatch,
        [
            FakeResponse(302, {"Location": "/final", "Set-Cookie": "sensitive=value"}),
            FakeResponse(200, {"Content-Type": "text/html"}, b"<h1>Example</h1>"),
        ],
    )
    fetcher = runner.Fetcher(frozenset({"https://example.com"}), timeout_seconds=30)
    url, status, media_type, content = fetcher.get("https://example.com/start")
    assert (url, status, media_type, content) == (
        "https://example.com/final",
        200,
        "text/html",
        b"<h1>Example</h1>",
    )
    assert [item["path"] for item in calls] == ["/start", "/final"]
    assert all(item["address"] == "93.184.216.34" for item in calls)
    assert all(
        "Cookie" not in item["headers"] and "Authorization" not in item["headers"] for item in calls
    )


@pytest.mark.parametrize("headers", [{"Content-Length": "3000000"}, {"Content-Encoding": "gzip"}])
def test_large_or_compressed_responses_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    calls = fake_connections(monkeypatch, [FakeResponse(headers=headers)])
    fetcher = runner.Fetcher(frozenset({"https://example.com"}), timeout_seconds=30)
    with pytest.raises(ValueError):
        fetcher.get("https://example.com/")
    assert calls[0]["closed"]


def test_redirect_chain_has_a_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_connections(
        monkeypatch, [FakeResponse(302, {"Location": "/loop"}) for _ in range(6)]
    )
    fetcher = runner.Fetcher(frozenset({"https://example.com"}), timeout_seconds=30)
    with pytest.raises(ValueError, match="redirect limit"):
        fetcher.get("https://example.com/start")
    assert len(calls) == 6


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("browser.read", {"url": "https://example.com", "cookies": []}),
        ("browser.read", {"url": "https://example.com", "max_text_chars": 16001}),
        ("browser.screenshot", {"url": "https://example.com", "output": "../shot.png"}),
        ("browser.render_html", {"input": "source.txt", "output": "shot.png"}),
        ("browser.render_html", {"input": "source.html", "output": "shot.jpg"}),
    ],
)
def test_no_model_scripts_credentials_or_unbounded_options(
    operation: str,
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        runner.validate_arguments(operation, arguments)


def test_existing_screenshot_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / "shot.png"
    output.write_bytes(b"existing")
    with pytest.raises(ValueError, match="already exists"):
        runner.workspace_file(tmp_path, "shot.png", output=True)
    assert output.read_bytes() == b"existing"


def test_write_dispatch_failure_is_uncertain_and_not_retried() -> None:
    manager, _, tool, context, transport = setup("browser.screenshot")
    manager.fail = True
    with pytest.raises(ToolExecutionError, match="Leased browser command failed") as error:
        transport(tool, {"url": "https://example.com", "output": "shot.png"}, context)
    assert error.value.unknown
    assert len(manager.calls) == 1
