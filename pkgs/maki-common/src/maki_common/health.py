"""Lightweight TCP health server for non-FastAPI services.

The legacy server returned `200 OK` to any request unconditionally. That is
indistinguishable from a wired-but-broken pod when used as a Kubernetes
readiness probe target — disconnected NATS, dead subscriptions, or a crashed
background task all sailed through.

Two related fixes live here now:

1. Per-service ``check`` callable. The callable returns
   ``(ok: bool, reason: str | None)``. When ``ok`` is ``False`` the response
   is a ``503 Service Unavailable`` with the reason in the JSON body. When no
   check is registered the server keeps the legacy "always 200" behaviour for
   callers that genuinely have no health state worth reporting.

2. HTTP path routing (issue #373). The old handler discarded the request line
   entirely, which meant ``GET /live``, ``GET /health`` and ``GET /nonexistent``
   all ran the same check. That collapsed liveness and readiness onto a single
   endpoint and is the architectural reason cortex/immune couldn't wire a
   separate ``livenessProbe`` (#276). The server now accepts a ``checks``
   mapping of URL path → check. Unregistered paths return ``404 Not Found`` so
   probe misconfiguration surfaces instead of masquerading as healthy. The
   single ``check`` API is retained as a whole-server fallback for backwards
   compatibility with pre-routing callers.
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

_NOT_FOUND_BODY = b'{"status":"not_found"}'
_NOT_FOUND_RESPONSE = (
    b"HTTP/1.1 404 Not Found\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_NOT_FOUND_BODY)).encode() + b"\r\n"
    b"\r\n" + _NOT_FOUND_BODY
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


def _parse_request_path(raw: bytes) -> str | None:
    """Extract the URL path from an HTTP request line.

    Returns ``None`` on malformed input. Only looks at the first CRLF-delimited
    line (the request line) — headers and body are ignored because we only need
    the path to route. Query string, if present, is stripped.
    """
    try:
        line_end = raw.find(b"\r\n")
        if line_end == -1:
            # Some minimal clients only send LF; be lenient so probes still route.
            line_end = raw.find(b"\n")
            if line_end == -1:
                return None
        parts = raw[:line_end].split(b" ")
        if len(parts) < 2:
            return None
        target = parts[1].decode("ascii", errors="replace")
        q = target.find("?")
        if q != -1:
            target = target[:q]
        return target or "/"
    except Exception:
        return None


def _make_handler(check: HealthCheck | None, checks: dict[str, HealthCheck] | None):
    routes: dict[str, HealthCheck] = dict(checks or {})
    fallback: HealthCheck | None = check
    has_routes = bool(routes)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.read(4096)

            # Nothing configured at all → legacy always-200 (vault sidecars etc).
            if not has_routes and fallback is None:
                writer.write(_OK_RESPONSE)
                await writer.drain()
                return

            path = _parse_request_path(raw)
            selected: HealthCheck | None = None

            if path is not None and path in routes:
                selected = routes[path]
            elif not has_routes:
                # Single-check overload: match every path (pre-routing behaviour).
                selected = fallback
            elif fallback is not None:
                # Route table + explicit fallback: unlisted paths hit the fallback.
                selected = fallback

            if selected is None:
                # Route table configured but path is unknown and no fallback given.
                # 404 on purpose — silent 200 on ``/nonexistent`` was a
                # probe-misconfiguration trap (issue #373).
                writer.write(_NOT_FOUND_RESPONSE)
                await writer.drain()
                return

            ok, reason = await _run_check(selected)
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
    checks: dict[str, HealthCheck] | None = None,
) -> asyncio.Server:
    """Start a minimal TCP health endpoint.

    Two composable APIs:

    * ``check`` — single callable used for every URL path. Backwards-compatible
      with pre-routing callers. When ``checks`` is *also* provided, ``check``
      acts as a fallback for paths not present in the mapping.
    * ``checks`` — mapping of URL path (e.g. ``"/live"``, ``"/health"``) to
      individual check callables. Requests for paths not in the mapping and
      not covered by ``check`` return **404 Not Found** so a mis-wired probe
      surfaces instead of silently succeeding.

    Each check returns ``(ok: bool, reason: str | None)``. When ``ok`` is
    ``False`` the response is a 503 with the reason in the JSON body. With no
    check configured, the server returns 200 unconditionally — only suitable
    for services that genuinely have no meaningful health state.

    Checks may be sync or async. Exceptions inside a check are converted to
    an unhealthy response rather than propagated, so a buggy check never
    crashes the listener.
    """
    handler = _make_handler(check, checks)
    server = await asyncio.start_server(handler, host, port)
    log.info(
        "Health server listening",
        extra={
            "host": host,
            "port": port,
            "single_check": check is not None,
            "routes": sorted(checks.keys()) if checks else [],
        },
    )
    return server
