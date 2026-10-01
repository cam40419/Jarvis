"""Bounded HTTPS retrieval and static rendering inside a disposable worker."""

from __future__ import annotations

import http.client
import importlib
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

OPERATIONS = frozenset({"browser.read", "browser.screenshot", "browser.render_html"})
_MAX_BODY = 2 * 1024 * 1024
_MAX_TOTAL = 10 * 1024 * 1024
_CSP = (
    "default-src 'none'; img-src data: https:; style-src 'unsafe-inline' https:; "
    "font-src data: https:; script-src 'none'; frame-src 'none'; object-src 'none'; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'"
)


def origin(url: Any) -> str:
    if (
        not isinstance(url, str)
        or len(url) > 4000
        or "\\" in url
        or any(ord(char) < 33 or ord(char) == 127 for char in url)
    ):
        raise ValueError("Expected a bounded HTTPS URL")
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", hostname) is None
    ):
        raise ValueError("Only HTTPS hostnames on port 443 without credentials are supported")
    return "https://" + hostname.lower()


def allowed_origins(value: Any) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("Configure up to sixteen exact HTTPS origins")
    result = set()
    for item in value:
        normalized = origin(item)
        parsed = urlsplit(item)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Allowed origins cannot contain paths, queries, or fragments")
        result.add(normalized)
    return frozenset(result)


def relative_file(value: Any, extensions: set[str]) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or value.startswith(("/", "-"))
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in value.split("/"))
        or Path(value).suffix.lower() not in extensions
    ):
        raise ValueError("Expected a relative workspace file with a supported extension")
    return value


def validate_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "browser.read": {"url", "max_text_chars"},
        "browser.screenshot": {"url", "output", "max_text_chars"},
        "browser.render_html": {"input", "output", "max_text_chars"},
    }
    if operation not in fields or set(arguments) - fields[operation]:
        raise ValueError("Unsupported browser operation or arguments")
    result = dict(arguments)
    if operation == "browser.render_html":
        result["input"] = relative_file(arguments.get("input"), {".html", ".htm"})
    else:
        origin(arguments.get("url"))
    if operation != "browser.read":
        result["output"] = relative_file(arguments.get("output"), {".png"})
    limit = arguments.get("max_text_chars", 8000)
    if type(limit) is not int or not 100 <= limit <= 16000:
        raise ValueError("max_text_chars must be between 100 and 16000")
    result["max_text_chars"] = limit
    return result


def workspace_file(workspace: Path, value: str, *, output: bool = False) -> Path:
    current = workspace
    for part in value.split("/"):
        current /= part
        if current.is_symlink():
            raise ValueError("Browser workspace files cannot be symbolic links")
    if not current.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("Browser file escaped its workspace")
    if output:
        if current.exists():
            raise ValueError("Screenshot already exists; choose a new filename")
    elif not current.is_file() or current.stat().st_size > _MAX_BODY:
        raise ValueError("Local HTML must be a regular file up to 2 MiB")
    return current


def public_address(hostname: str) -> str:
    records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    addresses = [str(record[4][0]) for record in records]
    if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
        raise ValueError("Browser destinations must resolve only to public Internet addresses")
    # Prefer IPv4 for worker hosts without IPv6 routing; the connection never
    # resolves the hostname again, so DNS rebinding cannot change the destination.
    return next((item for item in addresses if ":" not in item), addresses[0])


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, *, timeout: float) -> None:
        self.address = address
        self.ssl_context = ssl.create_default_context()
        super().__init__(hostname, 443, timeout=timeout, context=self.ssl_context)

    def connect(self) -> None:
        connection = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self.ssl_context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


class Fetcher:
    def __init__(self, origins: frozenset[str], *, timeout_seconds: int) -> None:
        self.origins = origins
        self.deadline = time.monotonic() + timeout_seconds
        self.requests = 0
        self.total_bytes = 0

    def get(self, url: str) -> tuple[str, int, str, bytes]:
        for _redirect in range(6):
            if origin(url) not in self.origins:
                raise ValueError("Browser URL or redirect is outside the configured origins")
            remaining = self.deadline - time.monotonic()
            self.requests += 1
            if remaining <= 0 or self.requests > 40:
                raise ValueError("Browser request/time limit exceeded")
            parsed = urlsplit(url)
            assert parsed.hostname is not None
            connection = PinnedHTTPSConnection(
                parsed.hostname,
                public_address(parsed.hostname),
                timeout=min(10, remaining),
            )
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": "SimonStaticBrowser/1.0",
                        "Accept-Encoding": "identity",
                        "Accept": "text/html,text/css,image/*,font/*;q=0.8,*/*;q=0.5",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Redirect omitted its destination")
                    url = urljoin(url, location)
                    continue
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed browser responses are unsupported")
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > _MAX_BODY):
                    raise ValueError("Browser response exceeds its size limit")
                content = response.read(_MAX_BODY + 1)
                self.total_bytes += len(content)
                if len(content) > _MAX_BODY or self.total_bytes > _MAX_TOTAL:
                    raise ValueError("Browser response budget exceeded")
                return (
                    url,
                    response.status,
                    response.getheader("Content-Type", "text/plain"),
                    content,
                )
            finally:
                connection.close()
        raise ValueError("Browser redirect limit exceeded")


def run(request: dict[str, Any], workspace: Path) -> dict[str, Any]:
    operation = request["operation"]
    arguments = validate_arguments(operation, request["arguments"])
    origins = allowed_origins(request["allowed_origins"])
    local = operation == "browser.render_html"
    target = (
        workspace_file(workspace, arguments["output"], output=True)
        if operation != "browser.read"
        else None
    )
    fetcher = Fetcher(origins, timeout_seconds=request["timeout_seconds"])
    if local:
        source = workspace_file(workspace, arguments["input"])
        content = source.read_bytes()
        initial_url, status, content_type = "https://simon-render.invalid/", 200, "text/html"
    else:
        initial_url, status, content_type, content = fetcher.get(arguments["url"])
        if content_type.split(";", 1)[0].lower() not in {
            "text/html",
            "text/plain",
            "application/xhtml+xml",
        }:
            raise ValueError("Browser reading requires an HTML or text document")
    playwright = importlib.import_module("playwright.sync_api")
    blocked = 0
    with playwright.sync_playwright() as provider:
        browser = provider.chromium.launch(
            headless=True,
            args=["--host-resolver-rules=MAP * ~NOTFOUND"],
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "LANG": "C.UTF-8",
            },
        )
        try:
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                java_script_enabled=False,
                service_workers="block",
                accept_downloads=False,
                permissions=[],
            )
            context.set_default_timeout(min(15000, request["timeout_seconds"] * 1000))
            delivered = False
            final_document_url, final_status = initial_url, status

            def route_resource(route: Any) -> None:
                nonlocal blocked, delivered, final_document_url, final_status
                resource = route.request
                if resource.method != "GET":
                    blocked += 1
                    route.abort()
                    return
                if not delivered and resource.url == initial_url:
                    delivered = True
                    result = (initial_url, status, content_type, content)
                elif local or resource.resource_type not in {
                    "document",
                    "stylesheet",
                    "image",
                    "font",
                }:
                    blocked += 1
                    route.abort()
                    return
                else:
                    try:
                        result = fetcher.get(resource.url)
                    except (ValueError, OSError, http.client.HTTPException):
                        blocked += 1
                        route.abort()
                        return
                if resource.is_navigation_request() and resource.frame.parent_frame is None:
                    final_document_url, final_status = result[0], result[1]
                route.fulfill(
                    status=result[1],
                    body=result[3],
                    headers={
                        "Content-Type": result[2],
                        "Content-Security-Policy": _CSP,
                        "Cache-Control": "no-store",
                    },
                )

            context.route("**/*", route_resource)
            page = context.new_page()
            page.goto(initial_url, wait_until="load")
            text = page.locator("body").inner_text()
            result = {
                "title": page.title()[:1000],
                "url": "local:" + arguments["input"] if local else final_document_url,
                "status": final_status,
                "text": text[: arguments["max_text_chars"]],
                "text_truncated": len(text) > arguments["max_text_chars"],
                "blocked_requests": blocked,
                "screenshot_path": None,
            }
            if target is not None:
                screenshot = page.screenshot(type="png", full_page=False, animations="disabled")
                if len(screenshot) > 10 * 1024 * 1024:
                    raise ValueError("Screenshot exceeded its size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(screenshot)
                result["screenshot_path"] = arguments["output"]
            return result
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        print(json.dumps(run(json.loads(sys.argv[1]), Path("/workspace")), ensure_ascii=False))
    except Exception:
        print(
            "Browser operation failed or was blocked by its destination/resource policy",
            file=sys.stderr,
        )
        sys.exit(2)
