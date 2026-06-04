"""Tests for ``maki_common.health.tcp_health_server``.

Focus: the path split between ``/live`` (always 200 — process is alive) and
``/health`` (runs the registered check). Issue #276 added this split so the
kubelet ``livenessProbe`` cannot crashloop the pod while the registered
readiness check legitimately returns 503 during dependency startup.

Drives asyncio directly via ``asyncio.run`` so pytest-asyncio is not required.
"""

from __future__ import annotations

import asyncio
import json

from maki_common.health import tcp_health_server


def _run(coro):
    return asyncio.run(coro)


async def _request(port: int, path: str) -> tuple[int, bytes]:
    """Send a minimal HTTP/1.1 GET and return (status_code, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read(4096)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    status = int(status_line.split(b" ")[1])
    return status, body


async def _serve(server: asyncio.Server) -> int:
    """Return the bound port for the running server."""
    return server.sockets[0].getsockname()[1]


# --- /live always returns 200, ignoring the check ----------------------------


def test_live_returns_200_when_check_fails() -> None:
    """/live must NOT consult the registered check.

    A failing check (NATS down, listener crashed, slow dep init) would make
    /health return 503. If /live were to share that signal, kubelet would
    SIGKILL the pod — exactly the crashloop pattern #253 / #276 fixes.
    """

    def always_fail() -> tuple[bool, str]:
        return False, "simulated dependency failure"

    async def scenario() -> None:
        server = await tcp_health_server(host="127.0.0.1", port=0, check=always_fail)
        try:
            port = await _serve(server)
            status, body = await _request(port, "/live")
            assert status == 200
            assert json.loads(body) == {"status": "alive"}
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_live_returns_200_when_check_raises() -> None:
    """An exception in the check still must not affect /live.

    Defensive: prior implementations could raise during socket teardown if
    the handler tried to inspect a check that blew up. /live must short-
    circuit before the check is ever invoked.
    """

    def raising_check() -> tuple[bool, str]:
        raise RuntimeError("check exploded")

    async def scenario() -> None:
        server = await tcp_health_server(host="127.0.0.1", port=0, check=raising_check)
        try:
            port = await _serve(server)
            status, body = await _request(port, "/live")
            assert status == 200
            assert json.loads(body) == {"status": "alive"}
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


# --- /health continues to honour the registered check ------------------------


def test_health_503_when_check_fails() -> None:
    async def scenario() -> None:
        server = await tcp_health_server(
            host="127.0.0.1",
            port=0,
            check=lambda: (False, "down"),
        )
        try:
            port = await _serve(server)
            status, body = await _request(port, "/health")
            assert status == 503
            payload = json.loads(body)
            assert payload["status"] == "unhealthy"
            assert payload["reason"] == "down"
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


def test_health_200_when_check_ok() -> None:
    async def scenario() -> None:
        server = await tcp_health_server(
            host="127.0.0.1",
            port=0,
            check=lambda: (True, None),
        )
        try:
            port = await _serve(server)
            status, body = await _request(port, "/health")
            assert status == 200
            assert json.loads(body) == {"status": "ok"}
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


# --- no check registered: legacy "always 200" behaviour ----------------------


def test_no_check_returns_200_for_any_path() -> None:
    async def scenario() -> None:
        server = await tcp_health_server(host="127.0.0.1", port=0, check=None)
        try:
            port = await _serve(server)
            for path in ("/", "/health", "/live", "/anything"):
                status, _ = await _request(port, path)
                assert status == 200, f"path {path} returned {status}"
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())


# --- query strings on /live must still route to alive ------------------------


def test_live_with_query_string_returns_alive() -> None:
    """kubelet sometimes adds query strings; the path parser must strip them."""

    async def scenario() -> None:
        server = await tcp_health_server(
            host="127.0.0.1",
            port=0,
            check=lambda: (False, "dep down"),
        )
        try:
            port = await _serve(server)
            status, body = await _request(port, "/live?probe=1")
            assert status == 200
            assert json.loads(body) == {"status": "alive"}
        finally:
            server.close()
            await server.wait_closed()

    _run(scenario())
