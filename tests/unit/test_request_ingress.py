import asyncio
from collections import deque

import pytest

from simon.api import request_ingress as ingress
from simon.api.request_ingress import RequestIngressMiddleware, SourceRateLimiter


def scope(path="/v1/example", *, method="POST", client="192.0.2.1", headers=(), root_path=""):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": root_path,
        "headers": list(headers),
        "client": (client, 1234) if client else None,
        "scheme": "https",
        "query_string": b"",
        "http_version": "1.1",
    }


def run(middleware, request=None, chunks=(b"hello",)):
    messages = deque(
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    )
    sent = []

    async def receive():
        return messages.popleft() if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(request or scope(), receive, send))
    return sent


@pytest.fixture
def app():
    bodies = []

    async def application(scope, receive, send):
        content = bytearray()
        while True:
            message = await receive()
            content.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        bodies.append(bytes(content))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return application, bodies


def test_chunked_overflow_never_reaches_a_mutating_handler(app):
    application, bodies = app
    middleware = RequestIngressMiddleware(application, default_max_body_bytes=8)
    sent = run(
        middleware,
        scope(headers=[(b"transfer-encoding", b"chunked")]),
        chunks=(b"1234", b"5678", b"9"),
    )
    assert sent[0]["status"] == 413 and not bodies


def test_dishonest_content_length_is_checked_against_received_bytes(app):
    application, bodies = app
    middleware = RequestIngressMiddleware(application, default_max_body_bytes=8)
    for content, status in ((b"123456789", 413), (b"1234", 400), (b"12", 400)):
        assert (
            run(middleware, scope(headers=[(b"content-length", b"3")]), chunks=(content,))[0][
                "status"
            ]
            == status
        )
    assert not bodies


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"-1")],
        [(b"content-length", b"nonsense")],
        [(b"content-length", b"2"), (b"content-length", b"3")],
        [(b"content-length", b"2"), (b"transfer-encoding", b"chunked")],
        [(b"content-length", b"9" * 100)],
    ],
)
def test_bad_framing_rejected_before_body_consumption(app, headers):
    application, bodies = app
    middleware = RequestIngressMiddleware(application)
    assert run(middleware, scope(headers=headers))[0]["status"] == 400
    assert not bodies


def test_boundary_body_replays_exact_bytes_and_spools_large_requests(app, monkeypatch):
    monkeypatch.setattr(ingress, "_SPOOL_MEMORY", 4)
    application, bodies = app
    middleware = RequestIngressMiddleware(application, default_max_body_bytes=12)
    assert run(middleware, chunks=(b"123", b"4567", b"89012"))[0]["status"] == 200
    assert bodies == [b"123456789012"]


@pytest.mark.parametrize(
    ("path", "root"),
    [
        ("/simon/auth/password/login", ""),
        ("/simon/auth/password/login", "/simon"),
        ("/auth/password/login", "/simon"),
        ("/simon/auth/password/login/", ""),
    ],
)
def test_public_prefix_and_mount_root_do_not_bypass_auth_cap(app, path, root):
    application, bodies = app
    middleware = RequestIngressMiddleware(application, public_path="/simon", auth_max_body_bytes=4)
    assert run(middleware, scope(path, root_path=root), chunks=(b"12345",))[0]["status"] == 413
    assert not bodies


def test_route_caps_cover_current_uploads_and_text_actions_only(app):
    middleware = RequestIngressMiddleware(app[0])
    assert middleware.limit_for("/v1/local-files/upload") == 50 * 1024 * 1024
    assert middleware.limit_for("/v1/projects/11111111-1111-4111-8111-111111111111/upload") == (
        2 * 1024 * 1024
    )
    assert middleware.limit_for("/v1/local-files/action") == 32 * 1024 * 1024
    assert middleware.limit_for("/v1/agent-platform/plans") == 2 * 1024 * 1024
    assert middleware.limit_for("/v1/unrelated/upload") == 2 * 1024 * 1024
    assert (
        run(
            middleware,
            scope(
                "/v1/local-files/upload",
                headers=[
                    (b"content-length", str(50 * 1024 * 1024 + 1).encode()),
                ],
            ),
        )[0]["status"]
        == 413
    )


def test_auth_limiter_ignores_spoofed_forwarding_and_keeps_other_peers_independent(app):
    middleware = RequestIngressMiddleware(app[0], auth_rate_limit=1, auth_rate_window_seconds=60)
    path = "/auth/passkeys/login/options"
    assert (
        run(middleware, scope(path, headers=[(b"x-forwarded-for", b"198.51.100.1")]))[0]["status"]
        == 200
    )
    denied = run(
        middleware,
        scope(
            path,
            headers=[
                (b"x-forwarded-for", b"198.51.100.2"),
                (b"cf-connecting-ip", b"198.51.100.3"),
                (b"forwarded", b"for=198.51.100.4"),
            ],
        ),
    )
    assert denied[0]["status"] == 429
    assert dict(denied[0]["headers"])[b"retry-after"] == b"60"
    assert run(middleware, scope(path, client="192.0.2.2"))[0]["status"] == 200
    assert len(app[1]) == 2


def test_loopback_tunnel_shares_bucket_and_ipv4_mapped_peer_cannot_evade_it(app):
    middleware = RequestIngressMiddleware(app[0], auth_rate_limit=1)
    path = "/auth/password/login"
    assert run(middleware, scope(path, client="127.0.0.1"))[0]["status"] == 200
    assert run(middleware, scope(path, client="::ffff:127.0.0.1"))[0]["status"] == 429


def test_limiter_refills_without_resetting_on_rejected_attempts_and_bounds_sources():
    clock = [0.0]
    limiter = SourceRateLimiter(2, 10, 1, lambda: clock[0])
    assert limiter.retry_after("first") is None
    assert limiter.retry_after("first") is None
    assert limiter.retry_after("first") == 5
    assert limiter.retry_after("new-source") is None
    assert limiter.retry_after("another-new-source") is None
    assert limiter.retry_after("third-new-source") == 5
    assert len(limiter._buckets) == 1
    clock[0] = 5
    assert limiter.retry_after("first") is None
    assert limiter.retry_after("first") == 5
    clock[0] = 20
    assert limiter.retry_after("replacement") is None
    assert list(limiter._buckets) == ["replacement"]


def test_body_timeout_has_no_partial_handler_effect(app):
    application, bodies = app
    middleware = RequestIngressMiddleware(application, body_timeout_seconds=0.001)
    sent = []

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"late", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope(), receive, send))
    assert sent[0]["status"] == 408 and not bodies


def test_trickled_body_does_not_restart_the_total_deadline(app):
    application, bodies = app
    middleware = RequestIngressMiddleware(application, body_timeout_seconds=0.06)
    sent, received = [], []

    async def receive():
        await asyncio.sleep(0.015)
        received.append(True)
        return {"type": "http.request", "body": b"x", "more_body": len(received) < 20}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope(), receive, send))
    assert sent[0]["status"] == 408 and not bodies
    assert 1 <= len(received) < 20


def test_downloads_and_sse_responses_pass_through_without_buffering_or_auth_rate_limit():
    sent = []

    async def streaming(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for chunk in (b"data: first\n\n", b"data: second\n\n"):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            assert sent[-1]["body"] == chunk
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive():
        pytest.fail("GET response streams must not be read into middleware buffers")

    async def send(message):
        sent.append(message)

    middleware = RequestIngressMiddleware(streaming, auth_rate_limit=1)
    asyncio.run(middleware(scope("/v1/download", method="GET"), receive, send))
    assert len(sent) == 4


def test_zero_rate_limit_is_explicitly_supported_for_controlled_tests(app):
    middleware = RequestIngressMiddleware(app[0], auth_rate_limit=0)
    for _ in range(3):
        assert run(middleware, scope("/auth/dev-login"))[0]["status"] == 200
