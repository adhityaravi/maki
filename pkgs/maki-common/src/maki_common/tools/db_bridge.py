"""Generic DB query bridge — routes read-only SQL from cortex to stem via NATS.

Same pattern as discord_search.py / trading_bridge.py: cortex calls the tool,
the request is forwarded via NATS to stem (which holds the asyncpg pool), and
the formatted result flows back.

Safety: stem validates SELECT-only + injects LIMIT 50.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from maki_common.subjects import DB_QUERY
from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

_QUERY_TIMEOUT = 15.0  # seconds


def make_db_query_tools(nc: Any) -> list[tuple[str, str, dict, Any]]:
    """Return the query_db bridge tool tuple for cortex."""

    async def query_db(args: dict[str, Any]) -> dict[str, Any]:
        sql = args.get("sql", "").strip()
        if not sql:
            return mcp_result("sql is required — provide a SELECT query.")

        payload = {"sql": sql}
        log.info("DB query bridge call", extra={"sql": sql[:120]})

        try:
            resp = await nc.request(
                DB_QUERY,
                json.dumps(payload).encode(),
                timeout=_QUERY_TIMEOUT,
            )
            data = json.loads(resp.data.decode())
        except Exception as exc:
            log.warning("DB query request failed", extra={"error": str(exc)})
            return mcp_result(f"DB query unavailable: {exc}")

        return data

    return [
        (
            "query_db",
            (
                "Run a read-only SQL query against maki-vault (PostgreSQL). "
                "Only SELECT and WITH (CTE) queries are allowed. "
                "Results are capped at 50 rows. "
                "Use this to inspect trade history, asset config, or any other "
                "data stored in the database.\n\n"
                "Tables available:\n"
                "- trade_signals: all accepted/skipped trade proposals\n"
                "- trade_outcomes: closed trade results (P&L)\n"
                "- asset_config: per-asset Kelly stats and indicator weights\n"
                "- memories: mem0 memory store\n"
            ),
            {"sql": str},
            query_db,
        )
    ]
