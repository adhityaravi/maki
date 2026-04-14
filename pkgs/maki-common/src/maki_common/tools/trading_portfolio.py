"""Always-on trading portfolio tools — read-only KV access, works 24/7.

These tools read portfolio state, open positions, trade history, and watchlist
directly from NATS KV.  They do NOT require a running trading loop — unlike the
loop-specific tools (fetch_news, get_price_action, etc.) that need live market
context.

Stem registers these as permanent handlers so cortex can query portfolio state
at any time via the ``trading_tool`` bridge.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

# KV key conventions — must match maki-loops trading modules
_KV_PORTFOLIO = "trading.portfolio"
_KV_CONFIG = "trading.config"
_KV_OPEN_PREFIX = "trading.open"
_KV_STATS_PREFIX = "trading.stats"


def _safe(symbol: str) -> str:
    """Sanitise a symbol for use as a KV key segment (``/`` → ``-``)."""
    return symbol.replace("/", "-")


def make_trading_portfolio_tools(kv: Any) -> dict[str, Any]:
    """Return ``{name: async_handler}`` dict of always-on trading read tools.

    Args:
        kv: NATS KV bucket handle (the ``maki-lock`` bucket).
    """

    async def _load_symbols() -> list[str]:
        try:
            entry = await kv.get(_KV_CONFIG)
            cfg = json.loads(entry.value.decode())
        except Exception:
            cfg = {}
        return cfg.get("crypto", []) + cfg.get("eu_stocks", []) + cfg.get("us_stocks", [])

    async def _load_portfolio_eur() -> float:
        try:
            entry = await kv.get(_KV_PORTFOLIO)
            return float(entry.value.decode())
        except Exception:
            return 100.0

    async def _scan_open_trades(symbols: list[str]) -> list[dict]:
        trades: list[dict] = []
        for sym in symbols:
            try:
                entry = await kv.get(f"{_KV_OPEN_PREFIX}.{_safe(sym)}")
                trades.append(json.loads(entry.value.decode()))
            except Exception:
                pass
        return trades

    # ── Handlers ──────────────────────────────────────────────────────────

    async def get_portfolio_summary(args: dict[str, Any]) -> dict[str, Any]:
        portfolio_eur = await _load_portfolio_eur()
        symbols = await _load_symbols()
        trades = await _scan_open_trades(symbols)
        deployed = sum(t.get("size_eur", 0) for t in trades)
        available = portfolio_eur - deployed
        pct = (deployed / portfolio_eur * 100) if portfolio_eur > 0 else 0.0
        lines = [
            "Portfolio summary:",
            f"  Total value: €{portfolio_eur:.2f}",
            f"  Deployed: €{deployed:.2f} ({pct:.1f}%)",
            f"  Available: €{available:.2f}",
            f"  Open positions: {len(trades)}",
        ]
        return mcp_result("\n".join(lines))

    async def get_open_positions(args: dict[str, Any]) -> dict[str, Any]:
        symbols = await _load_symbols()
        trades = await _scan_open_trades(symbols)
        if not trades:
            return mcp_result("No open positions.")
        now = datetime.now(UTC)
        lines = [f"Open positions ({len(trades)}):"]
        total = 0.0
        for t in trades:
            opened = datetime.fromisoformat(t["opened_at"])
            age_h = (now - opened).total_seconds() / 3600
            lines.append(
                f"- {t['symbol']}: {t['direction'].upper()} "
                f"@ {t['entry_price']:.2f}, €{t['size_eur']:.2f}, "
                f"open {age_h:.1f}h"
            )
            total += t.get("size_eur", 0)
        lines.append(f"\nTotal deployed: €{total:.2f}")
        return mcp_result("\n".join(lines))

    async def get_trade_history(args: dict[str, Any]) -> dict[str, Any]:
        symbol = args.get("symbol", "").strip().upper()
        if not symbol:
            return mcp_result("symbol is required")
        try:
            entry = await kv.get(f"{_KV_STATS_PREFIX}.{_safe(symbol)}")
            h = json.loads(entry.value.decode())
        except Exception:
            return mcp_result(f"No trade history for {symbol}.")
        wins = h.get("wins", 0)
        losses = h.get("losses", 0)
        total = wins + losses
        wr = wins / total if total > 0 else 0.5
        tg = h.get("total_gain_pct", 0)
        tl = h.get("total_loss_pct", 0)
        ratio = (tg / wins) / (tl / losses) if wins > 0 and losses > 0 and tl > 0 else 1.5
        lines = [
            f"Trade history for {symbol}:",
            f"  Total trades: {total}",
            f"  Wins: {wins} | Losses: {losses}",
            f"  Win rate: {wr:.0%}",
            f"  Avg win/loss ratio: {ratio:.2f}x",
        ]
        if total < 10:
            lines.append("  (Using seed priors — insufficient history)")
        return mcp_result("\n".join(lines))

    async def get_watchlist(args: dict[str, Any]) -> dict[str, Any]:
        try:
            entry = await kv.get(_KV_CONFIG)
            cfg = json.loads(entry.value.decode())
        except Exception:
            return mcp_result("No watchlist configured.")
        crypto = cfg.get("crypto", [])
        eu = cfg.get("eu_stocks", [])
        us = cfg.get("us_stocks", [])
        lines = [
            "Watchlist:",
            f"  Crypto: {', '.join(crypto) or 'none'}",
            f"  EU stocks: {', '.join(eu) or 'none'}",
            f"  US stocks: {', '.join(us) or 'none'}",
            f"  Total: {len(crypto) + len(eu) + len(us)} symbols",
        ]
        return mcp_result("\n".join(lines))

    return {
        "get_portfolio_summary": get_portfolio_summary,
        "get_open_positions": get_open_positions,
        "get_trade_history": get_trade_history,
        "get_watchlist": get_watchlist,
    }
