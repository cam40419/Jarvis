"""Run one local Uvicorn worker with an explicit graceful-stop request file."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
STOP_REQUEST = ROOT / ".local" / "simon-stop.request"


async def main() -> int:
    server = uvicorn.Server(
        uvicorn.Config(
            "simon.api.app:app",
            host="127.0.0.1",
            port=8000,
            access_log=False,
            proxy_headers=False,
        )
    )
    requested = False

    async def watch_stop_request() -> None:
        nonlocal requested
        while not server.should_exit:
            if STOP_REQUEST.exists():
                # Keep the request as an intentional-stop marker for recovery.
                # An explicit launcher clears it before migrations and setup.
                requested = True
                server.should_exit = True
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch_stop_request())
    listeners: list[socket.socket] = []
    try:
        # Windows resolves localhost to ::1 first. Listening on IPv4 alone adds a
        # connection timeout to every browser request before it falls back to IPv4.
        for family, address in (
            (socket.AF_INET, "127.0.0.1"),
            (socket.AF_INET6, "::1"),
        ):
            listener = socket.socket(family, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((address, 8000))
                listener.listen(2048)
                listener.setblocking(False)
            except BaseException:
                listener.close()
                raise
            listeners.append(listener)
        print("Simon listening on IPv4 and IPv6 localhost", flush=True)
        await server.serve(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
    return 0 if requested else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
