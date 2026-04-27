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


def _make_handler(check: HealthCheck | None):
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(4096)
            if check is None:
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

    If *check* is provided it is invoked on every request and must return
    ``(ok: bool, reason: str | None)``. When ``ok`` is ``False`` the response
    is a 503 with the reason in the JSON body. With no check, the server
    returns 200 unconditionally — only suitable for services that genuinely
    have no meaningful health state (vault sidecars, etc).

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
