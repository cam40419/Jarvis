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
        self.exit_code = 0
        self.truncated = False
        self.stdout = json.dumps(
            {
                "title": "Example",
                "url": "https://example.com/article",
                "status": 200,
                "text": "Verified source page contents",
                "text_truncated": False,
                "blocked_requests": 0,
                "links": [],
                "links_truncated": False,
                "screenshot_path": None,
            }
        )

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
        return ExecutionResult(
            exit_code=self.exit_code, stdout=self.stdout, truncated=self.truncated
        )


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
        workspace_id=request.workspace_id,
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
    assert all(tool.settings["public_web"] is False for tool in definitions)
    assert (
        "web.read" in next(tool for tool in definitions if tool.id == "browser.read").capabilities
    )


def test_transport_uses_owned_lease_and_only_configured_origins() -> None:
    manager, lease, tool, context, transport = setup()
    registry = TransportRegistry()
    registry.register("browser", transport)
    result = registry.execute(tool, {"url": "https://example.com/article"}, context)
    assert result.output["title"] == "Example"
    assert result.output["text"] == "Verified source page contents"
    assert result.output["url"] == "https://example.com/article"
    assert "stdout" not in result.output and "screenshot_path" not in result.output
    identifier, command, attempt, fence = manager.calls[0]
    assert (identifier, attempt, fence) == (lease.id, lease.plan.request.attempt_id, 8)
    assert command.argv[:3] == ("/usr/local/bin/python3", "-I", "-c")
    payload = json.loads(command.argv[4])
    assert payload["allowed_origins"] == ["https://example.com"]
    assert payload["arguments"]["max_text_chars"] == 8000
    assert payload["public_web"] is False


def test_public_web_is_an_operator_setting_not_a_model_permission() -> None:
    manager, _, tool, context, transport = setup()
    tool.settings.update({"allowed_origins": [], "public_web": True})
    transport(tool, {"url": "https://another-public.example/article"}, context)
    assert json.loads(manager.calls[0][1].argv[4])["public_web"] is True
    manager.calls.clear()
    with pytest.raises(ToolCatalogError):
        transport(tool, {"url": "https://example.com", "public_web": True}, context)
    assert manager.calls == []


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_public_web_requires_exact_boolean_operator_opt_in(value: Any) -> None:
    manager, _, tool, context, transport = setup()
    tool.settings["public_web"] = value
    with pytest.raises(ToolCatalogError, match="explicitly configured"):
        transport(tool, {"url": "https://example.com"}, context)
    with pytest.raises(ValueError, match="explicitly configured"):
        runner.Fetcher(frozenset(), timeout_seconds=30, public_web=value)
    assert manager.calls == []


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


@pytest.mark.parametrize("field", ["actor_id", "workspace_id", "run_id", "agent_id"])
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
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["224.0.0.1"],
        ["ff02::1"],
        ["93.184.216.34", "192.168.1.1"],
    ],
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


def test_public_web_redirects_pin_each_public_destination_without_credentials(monkeypatch):
    calls = fake_connections(
        monkeypatch,
        [
            FakeResponse(302, {"Location": "https://supplier.example/about"}),
            FakeResponse(200, {"Content-Type": "text/html"}, b"<p>Public facts</p>"),
        ],
    )
    hosts = []

    def address(hostname):
        hosts.append(hostname)
        return "93.184.216.34"

    monkeypatch.setattr(runner, "public_address", address)
    fetcher = runner.Fetcher(frozenset(), timeout_seconds=30, public_web=True)
    assert fetcher.get("https://brand.example/source")[0] == "https://supplier.example/about"
    assert hosts == ["brand.example", "supplier.example"]
    assert all(item["address"] == "93.184.216.34" and item["closed"] for item in calls)
    assert all(not {"Authorization", "Cookie"} & item["headers"].keys() for item in calls)


@pytest.mark.parametrize(
    "target", ["https://internal.example/", "https://169.254.169.254/", "https://mixed.example/"]
)
def test_public_web_redirect_cannot_reach_private_or_mixed_dns(monkeypatch, target):
    calls = fake_connections(monkeypatch, [FakeResponse(302, {"Location": target})])
    monkeypatch.setattr(runner, "public_address", runner_public_address)

    def dns(hostname, *args, **kwargs):
        addresses = ["93.184.216.34"] if hostname == "public.example" else ["169.254.169.254"]
        if hostname == "mixed.example":
            addresses.insert(0, "93.184.216.34")
        return [(2, 1, 6, "", (address, 443)) for address in addresses]

    monkeypatch.setattr(runner.socket, "getaddrinfo", dns)
    fetcher = runner.Fetcher(frozenset(), timeout_seconds=30, public_web=True)
    with pytest.raises(ValueError, match="public Internet"):
        fetcher.get("https://public.example/start")
    assert len(calls) == 1 and calls[0]["closed"]


runner_public_address = runner.public_address


@pytest.mark.parametrize(
    "target",
    [
        "http://example.com/",
        "https://user:secret@example.com/",
        "https://example.com:8443/",
        "file:///etc/passwd",
    ],
)
def test_public_web_does_not_bypass_url_policy(monkeypatch, target):
    calls = fake_connections(monkeypatch, [])
    fetcher = runner.Fetcher(frozenset(), timeout_seconds=30, public_web=True)
    with pytest.raises(ValueError):
        fetcher.get(target)
    assert calls == []


def test_outgoing_links_are_bounded_deduplicated_metadata():
    links, truncated = runner.outgoing_links(
        [
            {"href": "/manufacturing#details", "title": "  Production  "},
            {"href": "/manufacturing", "title": "duplicate"},
            {"href": "mailto:someone@example.com", "title": "Email"},
            {"href": "javascript:alert(1)", "title": "Script"},
            {"href": "https://user:secret@example.com/", "title": "Credentials"},
            {"href": "https://supplier.example/", "title": "Supplier " * 100},
        ],
        "https://brand.example/about",
    )
    assert not truncated
    assert links[0] == {"url": "https://brand.example/manufacturing", "title": "Production"}
    assert len(links) == 2 and len(links[1]["title"]) == 240
    links, truncated = runner.outgoing_links(
        [{"href": f"/page/{index}", "title": "Source"} for index in range(1000)],
        "https://brand.example/",
    )
    assert len(links) == 40 and truncated
    links, truncated = runner.outgoing_links(
        [{"href": f"/page/{index}/" + "a" * 3500, "title": "Source"} for index in range(40)],
        "https://brand.example/",
    )
    assert truncated and len(json.dumps(links, ensure_ascii=False).encode()) <= 16000


def test_http_error_page_is_not_reported_as_successful_content(monkeypatch, tmp_path):
    fake_connections(monkeypatch, [FakeResponse(403, {"Content-Type": "text/html"}, b"Forbidden")])
    with pytest.raises(ValueError, match="successful HTTP"):
        runner.run(
            {
                "operation": "browser.read",
                "arguments": {"url": "https://example.com/"},
                "allowed_origins": [],
                "public_web": True,
                "timeout_seconds": 30,
            },
            tmp_path,
        )


def test_nonzero_read_exit_is_known_tool_failure_and_not_retried():
    manager, _, tool, context, transport = setup()
    manager.exit_code = 2
    with pytest.raises(ToolExecutionError, match="could not be read") as error:
        transport(tool, {"url": "https://example.com"}, context)
    assert error.value.unknown is False
    assert len(manager.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"text": "x" * 16001},
        {"title": "x" * 1001},
        {"status": 403},
        {"url": "file:///etc/passwd"},
        {"text_truncated": "false"},
        {"links": [{"url": "https://example.com/", "title": "x"}] * 41},
        {"links": [{"url": "https://user:secret@example.com/", "title": "private"}]},
        {"screenshot_path": "output.png"},
        {"extra": "untrusted"},
    ],
)
def test_structured_read_result_rejects_malformed_or_unbounded_fields(change):
    manager, _, tool, context, transport = setup()
    page = json.loads(manager.stdout)
    page.update(change)
    manager.stdout = json.dumps(page)
    with pytest.raises(ToolExecutionError, match="incomplete or invalid") as error:
        transport(tool, {"url": "https://example.com"}, context)
    assert not error.value.unknown and len(manager.calls) == 1


@pytest.mark.parametrize(
    "output",
    ["broken JSON", '{"status":200,"status":201}', "{" * 5000, "x" * 131073],
    ids=["invalid-json", "duplicate-field", "nested-json", "oversized"],
)
def test_structured_read_result_rejects_invalid_serialization(output):
    manager, _, tool, context, transport = setup()
    manager.stdout = output
    with pytest.raises(ToolExecutionError, match="incomplete or invalid") as error:
        transport(tool, {"url": "https://example.com"}, context)
    assert not error.value.unknown


def test_transport_truncation_cannot_be_a_successful_page():
    manager, _, tool, context, transport = setup()
    manager.truncated = True
    with pytest.raises(ToolExecutionError, match="incomplete or invalid"):
        transport(tool, {"url": "https://example.com"}, context)


def test_structured_page_context_retains_source_identity_and_retrievable_text():
    from simon.services.worker_context import ToolEvidenceBuffer, render_history

    manager, _, tool, context, transport = setup()
    page = json.loads(manager.stdout)
    page["text"] = "Costing evidence; " * 800
    manager.stdout = json.dumps(page)
    output = transport(
        tool, {"url": "https://example.com/article", "max_text_chars": 16000}, context
    )
    entry = {
        "tool_id": tool.id,
        "invocation_id": str(uuid4()),
        "status": "succeeded",
        "side_effect": False,
        "arguments": {"url": page["url"]},
        "output": output,
    }
    evidence = ToolEvidenceBuffer()
    _, reference = evidence.capture(entry)
    entry["evidence"] = reference
    view = render_history([entry], 2500)
    assert view.compacted
    compacted = json.loads(view.text)["calls"][0]["output"]
    assert compacted["url"] == page["url"] and compacted["title"] == page["title"]
    assert compacted["text_truncated"] is False
    assert compacted["text"]["evidence_pointer"] == "/output/text"
    reread = evidence.read(
        {
            "invocation_id": entry["invocation_id"],
            "pointer": "/output/text",
            "offset": 0,
            "limit": 1000,
        }
    )
    assert reread["text"] == page["text"][:1000]


@pytest.mark.parametrize("operation", ["browser.screenshot", "browser.render_html"])
def test_image_operations_keep_execution_envelope(operation):
    manager, _, tool, context, transport = setup(operation)
    arguments = {"output": "image.png"}
    arguments.update(
        {"input": "source.html"}
        if operation == "browser.render_html"
        else {"url": "https://example.com"}
    )
    registry = TransportRegistry()
    registry.register("browser", transport)
    output = registry.execute(tool, arguments, context).output
    assert output == {"exit_code": 0, "stdout": manager.stdout, "stderr": "", "truncated": False}


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
