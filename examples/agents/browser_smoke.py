"""Exercise offline HTML rendering and allowlisted public HTTPS browsing in Docker."""

import argparse
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from simon.adapters.browser_tools import (
    BROWSER_CAPABILITIES,
    BrowserToolTransport,
    browser_tool_definitions,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.execution import EnvironmentDefinition, EnvironmentRequest, ExecutionCommand
from simon.domain.tool_catalog import ToolExecutionContext
from simon.services.execution import EnvironmentManager


def smoke(*, online: bool) -> None:
    state = (Path(".local/agents") / ("browser-smoke-" + uuid4().hex)).resolve()
    network: Literal["none", "bridge"] = "bridge" if online else "none"
    manager = EnvironmentManager(
        [
            EnvironmentDefinition(
                id="browser-smoke",
                kind="docker",
                enabled=True,
                container_image="simon-browser:local",
                capabilities=BROWSER_CAPABILITIES,
                network=network,
            )
        ],
        state_path=state / "leases.sqlite3",
        workspace_root=state / "workspaces",
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(),
        agent_id="browser-smoke",
        task_id=uuid4(),
        attempt_id=uuid4(),
        capabilities=BROWSER_CAPABILITIES,
        os="linux",
    )
    lease = manager.allocate(request, environment_id="browser-smoke")
    ownership = {"attempt_id": request.attempt_id, "fencing_token": lease.fencing_token}
    context = ToolExecutionContext(
        actor_id=uuid4(),
        household_id=request.workspace_id,
        run_id=uuid4(),
        agent_id="browser-smoke",
        scopes=frozenset({"jobs:read", "jobs:write"}),
        allowed_tool_ids=frozenset(item.id for item in browser_tool_definitions()),
        environment_capabilities=BROWSER_CAPABILITIES,
        authorized_action="write",
    )
    registry = TransportRegistry()
    registry.register(
        "browser",
        BrowserToolTransport(
            manager,
            lease,
            actor_id=context.actor_id,
            run_id=context.run_id,
        ),
    )
    definitions = {item.id: item for item in browser_tool_definitions(enabled=True)}
    try:
        if online:
            for operation in ("browser.read", "browser.screenshot"):
                definitions[operation].settings["allowed_origins"] = ["https://example.com"]
                arguments = {"url": "https://example.com/"}
                if operation == "browser.screenshot":
                    arguments["output"] = "example.png"
                result = registry.execute(definitions[operation], arguments, context)
                if result.output["exit_code"]:
                    raise RuntimeError(f"{operation}: {result.output['stderr']}")
                content = json.loads(result.output["stdout"])
                if content["title"] != "Example Domain" or not content["text"]:
                    raise RuntimeError("Public-page text did not match the expected fixture")
                print(f"{operation}: passed")
        else:
            html = (
                "<html><head><title>Simon render test</title></head>"
                "<body style='font:40px sans-serif'><h1>Offline preview</h1>"
                "<script>document.body.textContent='UNSAFE SCRIPT RAN'</script>"
                "<img src='https://example.com/blocked.png'></body></html>"
            )
            fixture = (
                "from pathlib import Path; Path('/workspace/source.html').write_text("
                + repr(html)
                + ")"
            )
            created = manager.execute(
                lease.id,
                ExecutionCommand(
                    argv=(
                        "/usr/local/bin/python3",
                        "-I",
                        "-c",
                        fixture,
                    )
                ),
                **ownership,
            )
            if created.exit_code:
                raise RuntimeError("Could not create synthetic HTML input")
            result = registry.execute(
                definitions["browser.render_html"],
                {"input": "source.html", "output": "preview.png"},
                context,
            )
            if result.output["exit_code"]:
                raise RuntimeError("browser.render_html: " + result.output["stderr"])
            content = json.loads(result.output["stdout"])
            if "Offline preview" not in content["text"] or "UNSAFE SCRIPT RAN" in content["text"]:
                raise RuntimeError("The offline page scripts policy failed")
            if content["blocked_requests"] < 1 or content["screenshot_path"] != "preview.png":
                raise RuntimeError("The offline screenshot or resource policy failed")
            print("browser.render_html + scripts/network blocked: passed")
    finally:
        manager.release(lease.id, **ownership)
    print(f"Lease released. Synthetic workspace retained at {state}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--online",
        action="store_true",
        help="Also read/screenshot the public https://example.com page",
    )
    options = parser.parse_args()
    smoke(online=False)
    if options.online:
        smoke(online=True)
