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
    if not addresses or any(
        not (address := ipaddress.ip_address(item)).is_global
        or address.is_multicast
        or address.is_reserved
        for item in addresses
    ):
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
    def __init__(
        self, origins: frozenset[str], *, timeout_seconds: int, public_web: bool = False
    ) -> None:
        if type(public_web) is not bool:
            raise ValueError("Public-web access must be explicitly configured as a boolean")
        self.origins = origins
        self.public_web = public_web
        self.deadline = time.monotonic() + timeout_seconds
        self.requests = 0
        self.total_bytes = 0

    def get(self, url: str) -> tuple[str, int, str, bytes]:
        for _redirect in range(6):
            destination = origin(url)  # HTTPS/credentials/port validation also applies publicly.
            if not self.public_web and destination not in self.origins:
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


def outgoing_links(
    entries: list[dict[str, str]], base_url: str
) -> tuple[list[dict[str, str]], bool]:
    """Bounded link metadata only; following any link still repeats the full URL/DNS policy."""
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries[:201]:
        try:
            href = entry.get("href", "")
            if not href or len(href) > 4000:
                continue
            url = urljoin(base_url, href)
            origin(url)
            url = urlsplit(url)._replace(fragment="").geturl()
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        if len(links) == 40:
            return links, True
        candidate = {"url": url, "title": entry.get("title", "").strip()[:240]}
        if len(json.dumps([*links, candidate], ensure_ascii=False).encode("utf-8")) > 16000:
            return links, True
        links.append(candidate)
    return links, len(entries) > 200


_READ_DOCUMENT = r"""({limit}) => {
    const blocks = new Set(['ADDRESS','ARTICLE','BLOCKQUOTE','DD','DIV','DL','DT',
        'FIGCAPTION','FIGURE','FOOTER','H1','H2','H3','H4','H5','H6','HEADER','HR','LI','MAIN','OL',
        'P','PRE','SECTION','TABLE','TR','UL']);
    const ignored = new Set(['NAV','SCRIPT','STYLE','NOSCRIPT',
        'TEMPLATE','SVG','CANVAS','IFRAME']);
    const cookieUI = new RegExp([
        '(?:^|\\s)(?:cookie[-_ ]?(?:banner|consent|notice|popup)|',
        'consent[-_ ]?(?:banner|dialog|notice)|onetrust-banner-sdk|',
        'onetrust-consent-sdk|cybotcookiebotdialog)(?:\\s|$)'
    ].join(''), 'i');
    function skip(element) {
        if (ignored.has(element.tagName) || element.hidden ||
            element.getAttribute('aria-hidden') === 'true') return true;
        const role = element.getAttribute('role');
        if (['navigation','banner','contentinfo'].includes(role)) return true;
        if (['HEADER','FOOTER'].includes(element.tagName) &&
            !element.closest('main, [role="main"], article')) return true;
        const identity = `${element.id} ${element.getAttribute('class') || ''}`;
        if (cookieUI.test(identity.replace(/\s+/g, ' '))) return true;
        if (role === 'dialog' && /cookies?|consent/i.test(
            `${element.getAttribute('aria-label') || ''} ${element.id}`)) return true;
        const style = getComputedStyle(element);
        return style.display === 'none' || style.visibility === 'hidden';
    }
    function extract(roots) {
        const parts = [], links = [];
        let characters = 0, visited = 0, textIncomplete = false, linksIncomplete = false;
        function add(value) {
            if (characters >= 200000) { textIncomplete = true; return; }
            const retained = value.slice(0, 200000 - characters);
            parts.push(retained); characters += retained.length;
            if (retained.length !== value.length) textIncomplete = true;
        }
        function walk(node, depth = 0) {
            if (++visited > 50000 || depth > 128) {
                textIncomplete = linksIncomplete = true; return;
            }
            if (node.nodeType === Node.TEXT_NODE) {
                add((node.nodeValue || '').replace(/\s+/g, ' ')); return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE || skip(node)) return;
            const tag = node.tagName;
            if (tag === 'BR' || blocks.has(tag)) add('\n');
            if (tag === 'A' && node.hasAttribute('href')) {
                if (links.length < 201) links.push({
                    href: (node.getAttribute('href') || '').slice(0, 4001),
                    title: (node.textContent || '').replace(/\s+/g, ' ').slice(0, 240)
                }); else linksIncomplete = true;
            }
            for (const child of node.childNodes) {
                if (visited >= 50000) { textIncomplete = linksIncomplete = true; break; }
                walk(child, depth + 1);
            }
            if (tag === 'TD' || tag === 'TH') add('\t');
            else if (blocks.has(tag)) add('\n');
        }
        for (const root of roots) {
            walk(root); if (visited >= 50000) break;
        }
        const text = parts.join('').replace(/[ \t]+\n/g, '\n')
            .replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
        return {text: text.slice(0, limit + 1), links, textIncomplete, linksIncomplete};
    }
    for (const selector of ['main', '[role="main"]', 'article']) {
        const found = Array.from(document.querySelectorAll(selector)).slice(0, 50)
            .filter(item => {
                for (let ancestor = item.parentElement; ancestor; ancestor = ancestor.parentElement)
                    if (skip(ancestor)) return false;
                return true;
            });
        const roots = found.filter(item => !found.some(
            parent => parent !== item && parent.contains(item)));
        const result = extract(roots);
        if (result.text) return result;
    }
    return extract(document.body ? [document.body] : []);
}"""


def read_document(page: Any, max_text_chars: int, base_url: str) -> dict[str, Any]:
    """Read semantic page content without changing the DOM used for screenshots."""
    extracted = page.evaluate(_READ_DOCUMENT, {"limit": max_text_chars})
    links, links_truncated = outgoing_links(extracted["links"], base_url)
    return {
        "text": extracted["text"][:max_text_chars],
        "text_truncated": len(extracted["text"]) > max_text_chars or extracted["textIncomplete"],
        "links": links,
        "links_truncated": links_truncated or extracted["linksIncomplete"],
    }


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
    fetcher = Fetcher(
        origins,
        timeout_seconds=request["timeout_seconds"],
        public_web=request.get("public_web", False),
    )
    if local:
        source = workspace_file(workspace, arguments["input"])
        content = source.read_bytes()
        initial_url, status, content_type = "https://simon-render.invalid/", 200, "text/html"
    else:
        initial_url, status, content_type, content = fetcher.get(arguments["url"])
        if operation == "browser.read" and not 200 <= status < 300:
            raise ValueError("The source page did not return a successful HTTP response")
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
            if operation == "browser.read" and not 200 <= final_status < 300:
                raise ValueError("The source page did not return a successful HTTP response")
            if operation == "browser.read":
                content_result = read_document(
                    page, arguments["max_text_chars"], final_document_url
                )
            else:
                text = page.locator("body").inner_text()
                content_result = {
                    "text": text[: arguments["max_text_chars"]],
                    "text_truncated": len(text) > arguments["max_text_chars"],
                    "links": [],
                    "links_truncated": False,
                }
            result = {
                "title": page.title()[:1000],
                "url": "local:" + arguments["input"] if local else final_document_url,
                "status": final_status,
                **content_result,
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
