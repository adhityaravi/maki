"""Memory search/feed/store helpers and the MEMORY_STORE NATS listener.

All functions here talk to ``maki-recall`` over HTTP — the caller supplies
the base URL and user id. Deduplication uses SequenceMatcher on memory
text, capped at ``max_count`` so the O(n²) cost stays tiny.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from difflib import SequenceMatcher

import httpx
from maki_common import spawn_background, subscribe_supervised
from maki_common.settings import RECALL_URL
from maki_common.subjects import MEMORY_STORE

# Owner identity for the whole memory pipeline lives in maki_common (see #160)
# so stem, cortex, and immune can't drift on who "adi" actually is. Re-exported
# here as a module-level name to keep the existing import surface (`from
# maki_stem.memory import MEMORY_USER_ID`) working.
from maki_common.tools.recall import MEMORY_USER_ID

log = logging.getLogger(__name__)

MEMORY_MAX_COUNT = int(os.environ.get("MEMORY_MAX_COUNT", "15"))
MEMORY_MIN_RELEVANCE = float(os.environ.get("MEMORY_MIN_RELEVANCE", "0.5"))


def deduplicate_memories(memories: list[dict], similarity_threshold: float = 0.82) -> list[dict]:
    """Remove near-duplicate memories, keeping the highest-scoring (first) copy.

    Uses SequenceMatcher ratio on memory text. O(n²) but n is capped at MEMORY_MAX_COUNT
    so this is always fast (~15 comparisons max).
    """
    unique: list[dict] = []
    for candidate in memories:
        text = candidate.get("text", "")
        is_dup = any(
            SequenceMatcher(None, text, existing.get("text", "")).ratio() >= similarity_threshold for existing in unique
        )
        if not is_dup:
            unique.append(candidate)
    return unique


async def search_memories(query: str) -> tuple[list[dict], list[str]]:
    """Query maki-recall for relevant memories and graph context.

    Fetches up to MEMORY_MAX_COUNT memories with score >= MEMORY_MIN_RELEVANCE.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{RECALL_URL}/search",
                json={"query": query, "user_id": MEMORY_USER_ID, "limit": MEMORY_MAX_COUNT * 2},
            )
            resp.raise_for_status()
            data = resp.json()

        memories = []
        for result in data.get("results", []):
            score = result.get("score", 0)
            if score >= MEMORY_MIN_RELEVANCE:
                memories.append(
                    {
                        "text": result.get("memory", ""),
                        "relevance": round(score, 2),
                    }
                )
        # Cap at max count (already sorted by score descending from recall)
        memories = memories[:MEMORY_MAX_COUNT]
        # Deduplicate near-identical memories (keeps highest-scoring, which come first)
        memories = deduplicate_memories(memories)

        graph_context = []
        skipped_dangling = 0
        for rel in data.get("relations", []):
            source = rel.get("source") or ""
            relationship = rel.get("relationship") or ""
            target = rel.get("target") or ""
            # Skip dangling or unresolved relationships — any missing endpoint is noise
            if not source or not relationship or not target or source == "?" or target == "?" or relationship == "?":
                skipped_dangling += 1
                continue
            graph_context.append(f"{source} --{relationship}--> {target}")
        if skipped_dangling:
            log.warning("Skipped dangling graph relations", extra={"count": skipped_dangling, "query": query})

        log.info(
            "Memory search complete",
            extra={"memories": len(memories), "relations": len(graph_context), "dangling_skipped": skipped_dangling},
        )
        return memories, graph_context

    except Exception:
        log.exception("Failed to search memories")
        return [], []


async def feed_memories(user_message: str, cortex_response: str) -> None:
    """Feed interaction to maki-recall for autonomous memory extraction."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{RECALL_URL}/memories",
                    json={
                        "messages": [
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": cortex_response},
                        ],
                        "user_id": MEMORY_USER_ID,
                    },
                )
                resp.raise_for_status()
                log.info("Memory feed complete", extra={"attempt": attempt + 1})
                return
        except httpx.ReadTimeout:
            log.warning("Memory feed timed out", extra={"attempt": attempt + 1})
            if attempt == 0:
                await asyncio.sleep(2.0)
        except Exception:
            log.exception("Failed to feed memories")
            return


async def _store_memory(content: str, source: str, user_id: str, metadata: dict | None) -> None:
    """Store a single memory via recall REST API (runs as background task)."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{RECALL_URL}/memories",
                    json={
                        "messages": [{"role": "user", "content": content}],
                        "user_id": user_id,
                        "metadata": metadata or {},
                    },
                )
                resp.raise_for_status()
                log.info("Memory stored", extra={"source": source, "attempt": attempt + 1})
                return
        except httpx.ReadTimeout:
            log.warning("Memory store timed out", extra={"source": source, "attempt": attempt + 1})
            if attempt == 0:
                await asyncio.sleep(2.0)
        except Exception:
            log.exception("Failed to store memory", extra={"source": source})
            return


async def _handle_memory_store(msg) -> None:
    """Spawn a background task to store one memory."""
    try:
        data = json.loads(msg.data.decode())
        content = data.get("content", "").strip()
        if not content:
            return

        user_id = data.get("user_id", MEMORY_USER_ID)
        source = data.get("source", "unknown")
        metadata = data.get("metadata")

        spawn_background(
            _store_memory(content, source, user_id, metadata),
            name="stem.store_memory",
        )
    except Exception:
        log.exception("Error in memory store listener")


async def memory_store_listener(nc, *, queue: str) -> None:
    """Listen for memory store requests from any component via NATS.

    Any component can publish to MEMORY_STORE with:
    {"content": "...", "user_id": "...", "metadata": {...}}
    Each memory is stored concurrently as a background task.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent memory
    write (issue #175).
    """
    await subscribe_supervised(
        nc,
        MEMORY_STORE,
        _handle_memory_store,
        queue=queue,
        name="stem.memory_store",
    )
