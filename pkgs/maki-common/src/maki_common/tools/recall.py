"""Memory tools — search, read, and store memories via maki-recall."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from maki_common.subjects import MEMORY_STORE

log = logging.getLogger(__name__)


def _mcp_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def make_recall_tools(recall_url: str) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for recall tools."""

    async def search_memories(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "")
        log.info("Tool: search_memories", extra={"query": query})
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{recall_url}/search",
                json={"query": query, "user_id": "adi"},
            )
            return _mcp_result(resp.text)

    async def get_all_memories(args: dict[str, Any]) -> dict[str, Any]:
        log.info("Tool: get_all_memories")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{recall_url}/memories", params={"user_id": "adi"})
            return _mcp_result(resp.text)

    async def add_memory(args: dict[str, Any]) -> dict[str, Any]:
        content = args.get("content", "")
        log.info("Tool: add_memory", extra={"content_len": len(content)})
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{recall_url}/memories",
                json={
                    "messages": [{"role": "assistant", "content": content}],
                    "user_id": "adi",
                },
            )
            return _mcp_result(resp.text)

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


def make_nats_memory_tools(nc: Any, source: str) -> list[tuple[str, str, dict[str, type], Any]]:
    """Memory store via NATS — for components without direct recall access.

    Publishes to MEMORY_STORE subject. Stem subscribes and forwards to recall.
    """

    async def store_memory(args: dict[str, Any]) -> dict[str, Any]:
        content = args.get("content", "")
        log.info("Tool: store_memory (NATS)", extra={"source": source, "content_len": len(content)})
        payload = {"content": content, "source": source, "user_id": "adi"}
        await nc.publish(MEMORY_STORE, json.dumps(payload).encode())
        return _mcp_result(f"Memory stored: {content[:100]}")

    return [
        (
            "store_memory",
            "Store a learning or observation into long-term memory. Use this when you discover "
            "something worth remembering — patterns, root causes, fixes, operational insights.",
            {"content": str},
            store_memory,
        ),
    ]
