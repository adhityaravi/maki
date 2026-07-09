"""Tests for ``maki_common.health.tcp_health_server``.

Focused on the path-routing behaviour introduced by issue #373: the old
handler ignored the request line entirely and returned the same response
to every URL, which made it impossible to wire distinct liveness and
readiness probes on a single port.

Drives asyncio directly with ``asyncio.run`` — no pytest-asyncio needed.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from maki_common.health import tcp_health_server


def _run(coro):
    return asyncio.run(coro)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _http_get(port: int, path: str) -> tuple[int, str]:
    """Open a TCP connection, send a minimal HTTP/1.1 GET, return (status, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
    writer.write(request)
    await writer.drain()
    raw = await reader.read(4096)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    text = raw.decode("utf-8", errors="replace")
    # Status line: "HTTP/1.1 <code> <reason>\r\n"
    status_line, _, rest = text.partition("\r\n")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 else 0
    body = rest.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rest else ""
    return status, body


def test_no_check_configured_returns_200_on_any_path() -> None:
    """Legacy always-200 behaviour when neither ``check`` nor ``checks`` is set."""

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(host="127.0.0.1", port=port)
        try:
            for path in ("/", "/live", "/health", "/nonexistent"):
                status, body = await _http_get(port, path)
                assert status == 200, (path, status, body)
                assert '"status":"ok"' in body
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_single_check_matches_every_path() -> None:
    """``check=...`` (no ``checks`` mapping) matches every URL — the pre-routing contract."""

    calls: list[Any] = []

    def check() -> tuple[bool, str | None]:
        calls.append(1)
        return True, None

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(host="127.0.0.1", port=port, check=check)
        try:
            for path in ("/", "/live", "/health", "/anything"):
                status, _ = await _http_get(port, path)
                assert status == 200, path
            assert len(calls) == 4
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_routes_dispatch_by_path() -> None:
    """Each path runs its own check; results are independent."""

    def live() -> tuple[bool, str | None]:
        return True, None

    def ready() -> tuple[bool, str | None]:
        return False, "warming up"

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(
            host="127.0.0.1",
            port=port,
            checks={"/live": live, "/health": ready},
        )
        try:
            status, body = await _http_get(port, "/live")
            assert status == 200, body
            assert '"status":"ok"' in body

            status, body = await _http_get(port, "/health")
            assert status == 503, body
            assert "warming up" in body
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_unknown_path_returns_404_when_routes_configured() -> None:
    """Unregistered path → 404 so probe misconfiguration surfaces (#373)."""

    def live() -> tuple[bool, str | None]:
        return True, None

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(host="127.0.0.1", port=port, checks={"/live": live})
        try:
            status, body = await _http_get(port, "/nonexistent")
            assert status == 404, body
            assert '"status":"not_found"' in body
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_query_string_is_stripped() -> None:
    """``/live?probe=k8s`` routes the same as ``/live``."""

    def live() -> tuple[bool, str | None]:
        return True, None

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(host="127.0.0.1", port=port, checks={"/live": live})
        try:
            status, _ = await _http_get(port, "/live?probe=k8s&t=1")
            assert status == 200
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_async_check_is_awaited() -> None:
    """Async checks are supported at every registered path."""

    async def live() -> tuple[bool, str | None]:
        await asyncio.sleep(0)
        return True, None

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(host="127.0.0.1", port=port, checks={"/live": live})
        try:
            status, _ = await _http_get(port, "/live")
            assert status == 200
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_check_exception_becomes_503() -> None:
    """A check that raises returns 503 with the exception summary in the body."""

    def boom() -> tuple[bool, str | None]:
        raise RuntimeError("kv gone")

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(host="127.0.0.1", port=port, checks={"/health": boom})
        try:
            status, body = await _http_get(port, "/health")
            assert status == 503, body
            assert "RuntimeError" in body and "kv gone" in body
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_routes_plus_fallback_check() -> None:
    """When both ``checks`` and ``check`` are given, ``check`` fills unlisted paths."""

    def live() -> tuple[bool, str | None]:
        return True, None

    def default() -> tuple[bool, str | None]:
        return False, "default fallback"

    async def scenario() -> None:
        port = _free_port()
        server = await tcp_health_server(
            host="127.0.0.1",
            port=port,
            check=default,
            checks={"/live": live},
        )
        try:
            status, _ = await _http_get(port, "/live")
            assert status == 200

            status, body = await _http_get(port, "/anything-else")
            assert status == 503
            assert "default fallback" in body
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())
