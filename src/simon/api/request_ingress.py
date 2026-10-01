"""Bound HTTP bodies and authentication work without buffering response streams."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from time import monotonic

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from simon.services.local_files import MAX_FILE

_PROJECT_UPLOAD = re.compile(r"^/v1/projects/[0-9a-fA-F-]{36}/upload$")
_LARGE_JSON = frozenset({"/v1/local-files/action", "/v1/agent-platform/plans"})
_SPOOL_MEMORY = 1024 * 1024


class SourceRateLimiter:
    """Bounded per-source token buckets; active entries cannot be evicted to evade limits."""

    def __init__(
        self, limit: int, window_seconds: float, max_sources: int, clock: Callable[[], float],
    ) -> None:
        if (type(limit) is not int or not 0 <= limit <= 10000
                or not math.isfinite(window_seconds) or not 0 < window_seconds <= 86400
                or type(max_sources) is not int or not 1 <= max_sources <= 65536):
            raise ValueError("Invalid authentication rate limits")
        self.limit, self.window_seconds, self.max_sources = limit, window_seconds, max_sources
        self.clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._overflow: tuple[float, float] | None = None
        self._lock = Lock()

    def retry_after(self, source: str) -> int | None:
        if self.limit == 0:
            return None
        now = self.clock()
        with self._lock:
            while self._buckets:
                first = next(iter(self._buckets))
                if self._buckets[first][1] > now - self.window_seconds:
                    break
                self._buckets.popitem(last=False)
            overflow = source not in self._buckets and len(self._buckets) >= self.max_sources
            previous = self._overflow if overflow else self._buckets.get(source)
            tokens, updated = previous if previous is not None else (float(self.limit), now)
            tokens = min(float(self.limit), tokens + max(0, now - updated)
                         * self.limit / self.window_seconds)
            retry = None if tokens >= 1 else max(1, math.ceil(
                (1 - tokens) * self.window_seconds / self.limit,
            ))
            state = (tokens - 1 if retry is None else tokens, now)
            if overflow:
                self._overflow = state
            else:
                self._buckets[source] = state
                self._buckets.move_to_end(source)
            return retry


class RequestIngressMiddleware:
    def __init__(
        self, app: ASGIApp, *, public_path: str = "", auth_rate_limit: int = 60,
        auth_rate_window_seconds: float = 60.0, auth_max_sources: int = 1024,
        auth_max_body_bytes: int = 65536, default_max_body_bytes: int = 2 * 1024 * 1024,
        local_upload_max_body_bytes: int = MAX_FILE, body_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        limits = (auth_max_body_bytes, default_max_body_bytes, local_upload_max_body_bytes)
        if any(type(value) is not int or not 1 <= value <= 64 * 1024 * 1024 for value in limits):
            raise ValueError("Ingress body limits must be between one byte and 64 MiB")
        if (not math.isfinite(body_timeout_seconds) or not 0 < body_timeout_seconds <= 3600
                or (public_path and not re.fullmatch(r"(?:/[a-zA-Z0-9_-]+)+", public_path))):
            raise ValueError("Invalid ingress timeout or public path")
        self.app, self.public_path = app, public_path
        self.auth_max_body_bytes, self.default_max_body_bytes = (
            auth_max_body_bytes, default_max_body_bytes,
        )
        self.local_upload_max_body_bytes = local_upload_max_body_bytes
        self.body_timeout_seconds = body_timeout_seconds
        self.limiter = SourceRateLimiter(
            auth_rate_limit, auth_rate_window_seconds, auth_max_sources, clock,
        )

    def path(self, scope: Scope) -> str:
        path = str(scope.get("path", "/"))
        root = str(scope.get("root_path", "")).rstrip("/")
        for prefix in dict.fromkeys((root, self.public_path)):
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                path = path[len(prefix):]
        return path.rstrip("/") or "/"

    def limit_for(self, path: str) -> int:
        if path == "/auth" or path.startswith("/auth/"):
            return self.auth_max_body_bytes
        if path == "/v1/local-files/upload":
            return self.local_upload_max_body_bytes
        if path in _LARGE_JSON:
            # Preserve the existing 2M-character local editor and 100-task plans,
            # including non-ASCII characters encoded as JSON surrogate escapes.
            return 32 * 1024 * 1024
        if _PROJECT_UPLOAD.fullmatch(path):
            return 16 * 1024 * 1024  # Existing 10 MiB binary upload plus base64 and JSON.
        return self.default_max_body_bytes

    @staticmethod
    def source(scope: Scope) -> str:
        # Never inspect Forwarded, X-Forwarded-For, CF-Connecting-IP or similar
        # client headers. Disable upstream ASGI proxy rewriting to retain the peer.
        client = scope.get("client")
        if not client:
            return "unknown-peer"
        try:
            address = ipaddress.ip_address(client[0])
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            return str(address)
        except ValueError:
            return "unknown-peer"

    @staticmethod
    def authentication_path(path: str) -> bool:
        return (path == "/auth/dev-login" or path == "/auth/password"
                or path.startswith(("/auth/password/", "/auth/passkeys/")))

    async def reject(
        self, scope: Scope, receive: Receive, send: Send, status: int, detail: str,
        *, retry: int | None = None,
    ) -> None:
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if retry is not None:
            headers["Retry-After"] = str(retry)
        response = JSONResponse({"detail": detail}, status_code=status, headers=headers)
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = self.path(scope)
        if scope["method"] == "POST" and self.authentication_path(path):
            retry = self.limiter.retry_after(self.source(scope))
            if retry is not None:
                await self.reject(scope, receive, send, 429, "Too many authentication requests",
                                  retry=retry)
                return
        limit = self.limit_for(path)
        lengths = [value for key, value in scope.get("headers", ()) if key == b"content-length"]
        chunked = any(key == b"transfer-encoding" for key, _ in scope.get("headers", ()))
        length = None
        if lengths:
            if (chunked or any(not value.isdigit() or len(value) > 20 for value in lengths)
                    or len(set(lengths)) != 1):
                await self.reject(scope, receive, send, 400, "Invalid request body framing")
                return
            length = int(lengths[0])
            if length > limit:
                await self.reject(scope, receive, send, 413,
                                  "Request body exceeds this route's limit")
                return
        if scope["method"] in {"GET", "HEAD", "OPTIONS"} and not chunked and not length:
            await self.app(scope, receive, send)
            return
        size = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.body_timeout_seconds
        # Spooling bounds middleware RAM and ensures an oversized chunked body
        # cannot reach a handler that would apply a partial mutation.
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY, mode="w+b") as body:
            while True:
                try:
                    remaining_seconds = deadline - loop.time()
                    if remaining_seconds <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(receive(), timeout=remaining_seconds)
                except TimeoutError:
                    await self.reject(scope, receive, send, 408, "Request body timed out")
                    return
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > limit:
                    await self.reject(scope, receive, send, 413,
                                      "Request body exceeds this route's limit")
                    return
                if size > _SPOOL_MEMORY:
                    await asyncio.to_thread(body.write, chunk)
                else:
                    body.write(chunk)
                if not message.get("more_body", False):
                    break
            if length is not None and size != length:
                await self.reject(scope, receive, send, 400, "Request body length does not match")
                return
            body.seek(0)
            remaining = size
            replay_done = False

            async def replay() -> Message:
                nonlocal remaining, replay_done
                if replay_done:
                    return await receive()
                count = min(65536, remaining)
                chunk = (await asyncio.to_thread(body.read, count) if size > _SPOOL_MEMORY
                         else body.read(count))
                remaining -= len(chunk)
                replay_done = remaining == 0
                return {"type": "http.request", "body": chunk, "more_body": not replay_done}

            await self.app(scope, replay, send)
