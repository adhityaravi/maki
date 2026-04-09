"""Discord history search tool — queries the ears leader via NATS request/reply.

Cortex calls `search_discord_history` when asked to recall something from chat
history. The request is forwarded to whichever ears instance currently holds
Discord leadership; results are relevance-ranked by Discord's server-side index.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from maki_common.subjects import EARS_SEARCH
from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 15.0  # seconds — covers Discord API latency + retry_after delays


def make_discord_search_tools(nc: Any) -> list[tuple[str, str, dict, Any]]:
    """Return the search_discord_history tool tuple for cortex."""

    async def search_discord_history(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "").strip()
        channel_id = args.get("channel_id")
        limit = max(1, min(int(args.get("limit", 25)), 25))

        if not query:
            return mcp_result("query is required")

        payload: dict[str, Any] = {"query": query, "limit": limit}
        if channel_id:
            payload["channel_id"] = channel_id

        log.info("Tool: search_discord_history", extra={"query": query[:80], "limit": limit})

        try:
            resp = await nc.request(EARS_SEARCH, json.dumps(payload).encode(), timeout=_SEARCH_TIMEOUT)
            data = json.loads(resp.data.decode())
        except Exception as exc:
            log.warning("Discord search request failed", extra={"error": str(exc)})
            return mcp_result(f"Discord search unavailable: {exc}")

        if not data.get("ok"):
            return mcp_result(f"Discord search error: {data.get('error', 'unknown')}")

        messages = data.get("messages", [])
        if not messages:
            return mcp_result(f"No Discord history found for: {query!r}")

        total = data.get("total_results", len(messages))
        lines = [f"Discord history — {query!r} ({total} result{'s' if total != 1 else ''})\n"]
        for m in messages:
            date = (m.get("timestamp") or "")[:10]
            author = m.get("author", "?")
            content = m.get("content", "")
            lines.append(f"[{date}] {author}: {content}")

        return mcp_result("\n".join(lines))

    return [
        (
            "search_discord_history",
            (
                "Search Discord channel history for past conversations. "
                "Uses Discord's server-side indexed search — relevance-ranked, not a linear scan. "
                "Use when asked to recall something discussed in chat, find a past decision, "
                "or when memory is fuzzy and the raw conversation trail would help. "
                "Optional: channel_id (int) to scope to a specific channel; limit 1–25 (default 25)."
            ),
            {"query": str, "channel_id": int | None, "limit": int | None},
            search_discord_history,
        )
    ]
