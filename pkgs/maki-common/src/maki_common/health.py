"""Lightweight TCP health server for non-FastAPI services.

The legacy server returned `200 OK` to any request unconditionally. That is
indistinguishable from a wired-but-broken pod when used as a Kubernetes
readiness probe target — disconnected NATS, dead subscriptions, or a crashed
background task all sailed through.

This module now supports a per-service ``check`` callable. The callable returns
``(ok: bool, reason: str | None)``. When ``ok`` is ``False`` the response is a
``503 Service Unavailable`` with the reason in the JSON body. When no check is
registered the server keeps the legacy "always 200" behaviour for callers that
genuinely have no health state worth reporting.

Path routing (issue #276): requests to ``/live`` always return 200 regardless
of the registered check. This is the kubelet ``livenessProbe`` target — it
answers "is this process alive and the event loop responsive?" without
involving any dependency state. ``/health`` (and any other path) runs the
check as before and is the ``readinessProbe`` / ``startupProbe`` target.

This split matters because readiness can legitimately stay red for minutes
while a slow dependency (Mem0, pgvector, Neo4j, NATS) wakes up. If liveness
shares the same signal the kubelet kills the pod just as it's about to come
up — the exact crashloop pattern #253 was opened to fix in maki-recall and
that this module brings to every TCP-served component (cortex, immune).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any, cast

log = logging.getLogger(__name__)

# A check returns (ok, reason). Sync or async are both accepted; the runtime
# awaits the result if it's awaitable. Typed as Any-returning to keep the
# user-facing ergonomics simple — most checks are sync lambdas.
HealthCheck = Callable[[], Any]

_OK_BODY = b'{"status":"ok"}'
_OK_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_OK_BODY)).encode() + b"\r\n"
    b"\r\n" + _OK_BODY
)

# Liveness response: distinct body so a `curl` against the pod immediately
# shows which probe was answered. The status code is what kubelet cares about
# either way, but the body helps humans during incident response.
_LIVE_BODY = b'{"status":"alive"}'
_LIVE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_LIVE_BODY)).encode() + b"\r\n"
    b"\r\n" + _LIVE_BODY
)


def _build_unhealthy_response(reason: str | None) -> bytes:
    body = json.dumps({"status": "unhealthy", "reason": reason or "unknown"}).encode()
    return (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )


async def _run_check(check: HealthCheck) -> tuple[bool, str | None]:
    """Invoke *check* (sync or async) and normalise exceptions to unhealthy."""
    try:
        raw = check()
        if inspect.isawaitable(raw):
            raw = await raw
        # The user contract is a 2-tuple (ok, reason).
        result = cast(tuple[Any, Any], raw)
        return bool(result[0]), result[1]
    except Exception as exc:  # noqa: BLE001 - intentional broad catch
        log.exception("Health check raised")
        return False, f"check raised: {type(exc).__name__}: {exc}"


def _parse_request_path(raw: bytes) -> str:
    """Extract the request path from an HTTP request line.

    Tolerant of malformed input — returns ``/`` if anything is off. The point
    is to distinguish ``GET /live`` from ``GET /health``, not to parse HTTP
    properly. Robustness matters because k8s probes are the noisiest source
    of malformed requests in practice (TCP connect tests, port scans, etc.).
    """
    try:
        line = raw.split(b"\r\n", 1)[0]
        parts = line.split(b" ", 2)
        if len(parts) < 2:
            return "/"
        # Strip query string — we only route on the path.
        path = parts[1].decode("ascii", errors="replace")
        return path.split("?", 1)[0] or "/"
    except Exception:
        return "/"


def _make_handler(check: HealthCheck | None):
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.read(4096)
            path = _parse_request_path(raw)
            # Liveness: process-only signal. Always 200 if we got here at all,
            # because "we're inside the handler" already proves the event loop
            # is turning. Crucial that this does NOT call the registered check
            # — the whole point of liveness is to survive a 503-on-readiness
            # window without the kubelet killing the pod.
            if path == "/live":
                writer.write(_LIVE_RESPONSE)
            elif check is None:
                writer.write(_OK_RESPONSE)
            else:
                ok, reason = await _run_check(check)
                if ok:
                    writer.write(_OK_RESPONSE)
                else:
                    writer.write(_build_unhealthy_response(reason))
            await writer.drain()
        except Exception:
            log.exception("Health handler failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    return _handle


async def tcp_health_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    check: HealthCheck | None = None,
) -> asyncio.Server:
    """Start a minimal TCP health endpoint.

    Routes by request path:

    * ``GET /live`` — always 200 with ``{"status":"alive"}``. Reaching the
      handler at all proves the event loop is responsive; we deliberately do
      NOT consult *check* so dependency outages cannot trigger a kubelet kill.
      This is the ``livenessProbe`` target.

    * ``GET /health`` (and any other path) — runs *check* if registered,
      returning 200 with ``{"status":"ok"}`` on healthy or 503 with the
      reason in the JSON body on unhealthy. With no *check* registered the
      legacy "always 200" behaviour is preserved. This is the
      ``readinessProbe`` / ``startupProbe`` target.

    *check* may be sync or async. Exceptions inside the check are converted
    to an unhealthy response rather than propagated, so a buggy check never
    crashes the listener.
    """
    handler = _make_handler(check)
    server = await asyncio.start_server(handler, host, port)
    log.info(
        "Health server listening",
        extra={"host": host, "port": port, "checked": check is not None},
    )
    return server
