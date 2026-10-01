import asyncio
import importlib.util
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("run_local", ROOT / "scripts/run_local.py")
assert SPEC and SPEC.loader
local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local)


@pytest.mark.parametrize("existing_request", [False, True])
def test_graceful_stop_closes_listeners_and_preserves_request_for_supervisor(
    tmp_path,
    monkeypatch,
    existing_request,
):
    marker = tmp_path / "simon-stop.request"
    if existing_request:
        marker.touch()
    listeners = []

    class Listener:
        def __init__(self, *args):
            self.closed = False
            listeners.append(self)

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            self.address = address

        def listen(self, backlog):
            pass

        def setblocking(self, blocking):
            pass

        def close(self):
            self.closed = True

    class Server:
        def __init__(self, config):
            assert marker.exists() == existing_request
            assert config["proxy_headers"] is False
            self.should_exit = False

        async def serve(self, *, sockets):
            assert len(sockets) == 2
            marker.touch()
            for _ in range(100):
                if self.should_exit:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Server did not receive its stop request")

    monkeypatch.setattr(local, "STOP_REQUEST", marker)
    monkeypatch.setattr(
        local,
        "socket",
        SimpleNamespace(
            **{
                name: getattr(socket, name)
                for name in (
                    "AF_INET",
                    "AF_INET6",
                    "SOCK_STREAM",
                    "SOL_SOCKET",
                    "SO_REUSEADDR",
                    "IPPROTO_IPV6",
                    "IPV6_V6ONLY",
                )
            },
            socket=Listener,
        ),
    )
    monkeypatch.setattr(
        local,
        "uvicorn",
        SimpleNamespace(
            Server=Server,
            Config=lambda *args, **kwargs: kwargs,
        ),
    )
    assert asyncio.run(local.main()) == 0
    assert marker.exists() and len(listeners) == 2
    assert all(listener.closed for listener in listeners)
