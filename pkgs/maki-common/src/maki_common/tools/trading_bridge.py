"""Trading tool bridge — routes tool calls from cortex to stem via NATS request/reply.

Same pattern as discord_search.py: cortex calls the tool, the request is
forwarded via NATS to whichever stem instance has the trading context, and the
result flows back.

The bridge exposes a single meta-tool ``trading_tool`` that dispatches to
specific handlers (fetch_news, get_price_action, etc.) on the stem side.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from maki_common.subjects import TRADING_TOOL_REQUEST
from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

_TOOL_TIMEOUT = 15.0  # seconds

# Tool descriptions exposed to Claude in the system prompt
TRADING_TOOL_DESCRIPTIONS = """\
Available trading analysis tools (call via trading_tool):
- fetch_news(symbol): Get recent headlines for a symbol from Finnhub.
- search_market_news(category): Get general market news. Categories: general, crypto, forex, merger.
- get_price_action(symbol, lookback_days): Get OHLCV summary. lookback_days: 1–30 (default 10).
- compute_support_resistance(symbol): Compute key support/resistance levels from daily candles.
- check_correlation(symbol1, symbol2): Check price return correlation between two symbols.
- get_trade_history(symbol): Get Kelly win/loss statistics for a symbol.
- get_trade_book(symbol): Full trade book with entries and computed position (units, avg cost, P&L).
- get_watchlist(): Current trading watchlist (crypto, EU stocks, US stocks).
- add_watchlist_symbol(symbol, category): Add a symbol. category: crypto, eu_stocks, us_stocks.
- remove_watchlist_symbol(symbol, category): Remove a symbol from the watchlist."""


def make_trading_bridge_tools(nc: Any) -> list[tuple[str, str, dict, Any]]:
    """Return the trading_tool bridge tuple for cortex."""

    async def trading_tool(args: dict[str, Any]) -> dict[str, Any]:
        tool_name = args.get("tool_name", "").strip()
        tool_args = args.get("tool_args", {})

        if not tool_name:
            return mcp_result("tool_name is required. " + TRADING_TOOL_DESCRIPTIONS)

        payload = {"tool_name": tool_name, "tool_args": tool_args}
        log.info("Trading tool bridge call", extra={"tool_name": tool_name})

        try:
            resp = await nc.request(
                TRADING_TOOL_REQUEST,
                json.dumps(payload).encode(),
                timeout=_TOOL_TIMEOUT,
            )
            data = json.loads(resp.data.decode())
        except Exception as exc:
            log.warning("Trading tool request failed", extra={"tool_name": tool_name, "error": str(exc)})
            return mcp_result(f"Trading tool unavailable: {exc}")

        return data

    return [
        (
            "trading_tool",
            (
                "Call a trading analysis tool on the stem side. "
                "Dispatches to specific handlers that have access to live market data, "
                "news feeds, portfolio state, and trade history.\n\n" + TRADING_TOOL_DESCRIPTIONS
            ),
            {"tool_name": str, "tool_args": dict},
            trading_tool,
        )
    ]
