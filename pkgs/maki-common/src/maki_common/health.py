"""Lightweight TCP health server for non-FastAPI services."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_RESPONSE_BODY = b'{"status":"ok"}'
_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_RESPONSE_BODY)).encode() + b"\r\n"
    b"\r\n" + _RESPONSE_BODY
)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(4096)
    writer.write(_RESPONSE)
    await writer.drain()
    writer.close()


async def tcp_health_server(host: str = "0.0.0.0", port: int = 8080) -> asyncio.Server:
    """Start a minimal TCP health endpoint returning {"status":"ok"}.

    Returns the server so the caller can await serve_forever() if needed.
    """
    server = await asyncio.start_server(_handle, host, port)
    log.info("Health server listening", extra={"host": host, "port": port})
    return server
