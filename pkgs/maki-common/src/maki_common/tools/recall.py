"""Memory tools — search, read, and store memories via maki-recall.

Owner identity (``MEMORY_USER_ID``) is resolved once from the environment so
every memory call — search, read, NATS-publish, REST-post — agrees on whose
memories it's talking about. Stem imports the same constant from here so the
env var flows through a single source of truth instead of being re-read (or
hardcoded) in each site. See issue #160.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from maki_common.subjects import MEMORY_STORE
from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

# Owner identity for every memory operation. Resolved once at import time —
# this is the single source of truth for the entire memory pipeline. Stem's
# ``memory.py`` imports it from here rather than re-reading the env var so
# the two sites can't drift.
MEMORY_USER_ID = os.environ.get("MEMORY_USER_ID", "adi")


def make_recall_tools(
    recall_url: str, nc: Any | None = None, source: str = "cortex"
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for recall tools.

    If nc is provided, add_memory fires asynchronously via NATS (instant return).
    Otherwise falls back to blocking REST call.
    """

    async def search_memories(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "")
        log.info("Tool: search_memories", extra={"query": query})
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{recall_url}/search",
                json={"query": query, "user_id": MEMORY_USER_ID},
            )
            return mcp_result(resp.text)

    async def get_all_memories(args: dict[str, Any]) -> dict[str, Any]:
        log.info("Tool: get_all_memories")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{recall_url}/memories", params={"user_id": MEMORY_USER_ID})
            return mcp_result(resp.text)

    if nc is not None:

        async def add_memory(args: dict[str, Any]) -> dict[str, Any]:
            content = args.get("content", "")
            log.info("Tool: add_memory (NATS)", extra={"content_len": len(content)})
            payload = {"content": content, "source": source, "user_id": MEMORY_USER_ID}
            await nc.publish(MEMORY_STORE, json.dumps(payload).encode())
            return mcp_result(f"Memory queued: {content[:100]}")

    else:

        async def add_memory(args: dict[str, Any]) -> dict[str, Any]:
            content = args.get("content", "")
            log.info("Tool: add_memory", extra={"content_len": len(content)})
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{recall_url}/memories",
                    json={
                        "messages": [{"role": "assistant", "content": content}],
                        "user_id": MEMORY_USER_ID,
                    },
                )
                return mcp_result(resp.text)

    return [
        (
            "search_memories",
            "Search your memories for information relevant to a query.",
            {"query": str},
            search_memories,
        ),
        (
            "get_all_memories",
            "Retrieve all stored memories.",
            {},
            get_all_memories,
        ),
        (
            "add_memory",
            "Store a new memory. Use this to remember important information.",
            {"content": str},
            add_memory,
        ),
    ]
