"""Always-on trading portfolio tools — read trade book + capital from NATS KV.

These tools work 24/7 (no running trading loop required). They read the
canonical trade book (``trading.book.{symbol}``), capital seed
(``trading.capital``), and watchlist (``trading.asset_config``) directly
from the shared KV, and compute positions using the same average-cost
basis as :mod:`maki_common.trading.book`.

Stem registers these as permanent handlers so Maki can query portfolio
state at any time via the ``trading_tool`` bridge, including outside of
a trading loop turn.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from maki_common.tools.utils import mcp_result
from maki_common.trading import (
    compute_position as _compute_position,
)
from maki_common.trading import (
    load_book as _load_book,
)
from maki_common.trading import (
    load_seed as _load_seed,
)
from maki_common.trading import (
    safe_symbol as _safe,
)

log = logging.getLogger(__name__)

# KV key conventions for keys not owned by maki_common.trading
_KV_ASSET_CONFIG = "trading.asset_config"
_KV_PRICE_PREFIX = "trading.price"  # trading.price.{symbol_safe}: {price, timestamp}


# ── KV readers ──────────────────────────────────────────────────────────────


async def _load_asset_config(kv: Any) -> dict[str, list[str]]:
    try:
        entry = await kv.get(_KV_ASSET_CONFIG)
        return json.loads(entry.value.decode())
    except Exception:
        return {}


async def _load_all_symbols(kv: Any) -> list[str]:
    cfg = await _load_asset_config(kv)
    return cfg.get("crypto", []) + cfg.get("eu_stocks", []) + cfg.get("us_stocks", [])


async def _load_price(kv: Any, symbol: str) -> tuple[float, str | None]:
    """Return ``(price, iso_timestamp)`` for *symbol* from the cache.

    The price is written by the trading loop each run; stale between runs.
    Returns ``(0.0, None)`` if no cache entry exists.
    """
    try:
        entry = await kv.get(f"{_KV_PRICE_PREFIX}.{_safe(symbol)}")
        data = json.loads(entry.value.decode())
        return float(data.get("price", 0) or 0), data.get("timestamp")
    except Exception:
        return 0.0, None


def _age_str(ts_iso: str | None) -> str:
    """Human-readable age of a cached timestamp, e.g. ``3h ago``."""
    if not ts_iso:
        return "unknown age"
    try:
        ts = datetime.fromisoformat(ts_iso)
    except ValueError:
        return "unknown age"
    delta = datetime.now(UTC) - ts
    mins = int(delta.total_seconds() / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins / 60
    if hours < 24:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


async def _load_all_positions(kv: Any) -> list[dict]:
    """Compute positions for every symbol that has a book entry."""
    symbols = await _load_all_symbols(kv)
    positions: list[dict] = []
    for sym in symbols:
        entries = await _load_book(kv, sym)
        if not entries:
            continue
        positions.append(_compute_position(sym, entries))
    return positions


# ── Handlers ────────────────────────────────────────────────────────────────


def make_trading_portfolio_tools(kv: Any) -> dict[str, Any]:
    """Return ``{name: async_handler}`` dict of always-on trading read tools.

    Args:
        kv: NATS KV bucket handle (the ``maki-lock`` bucket).
    """

    async def get_portfolio_summary(args: dict[str, Any]) -> dict[str, Any]:
        seed = await _load_seed(kv)
        positions = await _load_all_positions(kv)

        deployed = sum(p["avg_cost"] * p["net_units"] for p in positions if p["is_open"])
        realized = sum(p["realized_pnl"] for p in positions)
        total_value = seed + realized
        available = max(0.0, total_value - deployed)
        open_count = sum(1 for p in positions if p["is_open"])
        pct = (deployed / total_value * 100) if total_value > 0 else 0.0

        # Mark-to-market using cached prices from the last loop run
        mtm_value = 0.0
        unrealized = 0.0
        priced = 0
        oldest_ts: str | None = None
        for p in positions:
            if not p["is_open"]:
                continue
            price, ts = await _load_price(kv, p["symbol"])
            if price <= 0:
                continue
            mtm_value += price * p["net_units"]
            unrealized += (price - p["avg_cost"]) * p["net_units"]
            priced += 1
            if ts and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts

        r_sign = "+" if realized >= 0 else ""
        u_sign = "+" if unrealized >= 0 else ""
        lines = [
            "Portfolio summary:",
            f"  Seed: €{seed:.2f} (Adi adds via !trade addcash <amount>)",
            f"  Realised P&L: {r_sign}€{realized:.2f}",
            f"  Total pot (cost basis): €{total_value:.2f}",
            f"  Deployed: €{deployed:.2f} ({pct:.1f}%)",
            f"  Available cash: €{available:.2f}",
            f"  Open positions: {open_count}",
        ]
        if open_count > 0 and priced == open_count:
            total_mtm = available + mtm_value
            lines += [
                "",
                f"  Market value of holdings: €{mtm_value:.2f}  (prices {_age_str(oldest_ts)})",
                f"  Unrealised P&L: {u_sign}€{unrealized:.2f}",
                f"  Total portfolio (mark-to-market): €{total_mtm:.2f}",
            ]
        elif open_count > 0:
            lines.append(f"  (Live prices missing for {open_count - priced}/{open_count} positions)")
        return mcp_result("\n".join(lines))

    async def get_open_positions(args: dict[str, Any]) -> dict[str, Any]:
        positions = await _load_all_positions(kv)
        open_pos = [p for p in positions if p["is_open"]]
        if not open_pos:
            return mcp_result("No open positions.")

        lines = [f"Open positions ({len(open_pos)}):"]
        total_cost = 0.0
        total_mtm = 0.0
        total_upnl = 0.0
        have_all_prices = True
        oldest_ts: str | None = None
        for p in open_pos:
            cost = p["avg_cost"] * p["net_units"]
            total_cost += cost
            r_sign = "+" if p["realized_pnl"] >= 0 else ""
            realized_str = f", realised {r_sign}€{p['realized_pnl']:.2f}" if abs(p["realized_pnl"]) > 0.005 else ""

            price, ts = await _load_price(kv, p["symbol"])
            if price > 0:
                mtm = price * p["net_units"]
                upnl = (price - p["avg_cost"]) * p["net_units"]
                upnl_pct = (upnl / cost * 100) if cost > 0 else 0.0
                u_sign = "+" if upnl >= 0 else ""
                total_mtm += mtm
                total_upnl += upnl
                if ts and (oldest_ts is None or ts < oldest_ts):
                    oldest_ts = ts
                lines.append(
                    f"- {p['symbol']}: {p['net_units']:.6f} units @ avg "
                    f"€{p['avg_cost']:,.2f} → now €{price:,.2f} "
                    f"| P&L: {u_sign}€{upnl:.2f} ({u_sign}{upnl_pct:.1f}%){realized_str}"
                )
            else:
                have_all_prices = False
                lines.append(
                    f"- {p['symbol']}: {p['net_units']:.6f} units @ avg "
                    f"€{p['avg_cost']:,.2f} (cost basis €{cost:.2f}{realized_str}, price unknown)"
                )

        lines.append("")
        lines.append(f"Total cost basis: €{total_cost:.2f}")
        if have_all_prices:
            u_sign = "+" if total_upnl >= 0 else ""
            lines.append(f"Market value: €{total_mtm:.2f}  (prices {_age_str(oldest_ts)})")
            lines.append(f"Unrealised P&L: {u_sign}€{total_upnl:.2f}")
        return mcp_result("\n".join(lines))

    async def get_trade_book(args: dict[str, Any]) -> dict[str, Any]:
        symbol = args.get("symbol", "").strip().upper()
        if not symbol:
            return mcp_result("symbol is required")

        entries = await _load_book(kv, symbol)
        if not entries:
            return mcp_result(f"No trade book for {symbol}.")

        position = _compute_position(symbol, entries)
        lines = [
            f"Trade book for {symbol} ({len(entries)} entries):",
            f"  Net units: {position['net_units']:.6f}",
            f"  Avg cost: €{position['avg_cost']:,.2f}",
            f"  Bought: €{position['total_bought_eur']:.2f}",
            f"  Sold: €{position['total_sold_eur']:.2f}",
            f"  Realised P&L: €{position['realized_pnl']:.2f}",
            f"  Status: {'OPEN' if position['is_open'] else 'FLAT'}",
            "",
            "Recent entries:",
        ]
        for e in entries[-10:]:
            ts = str(e.get("timestamp", ""))[:16]
            direction = str(e.get("direction", "")).upper()
            price = float(e.get("price", 0) or 0)
            size_eur = float(e.get("size_eur", 0) or 0)
            units = size_eur / price if price > 0 else 0.0
            lines.append(f"  {ts} {direction} €{size_eur:.2f} @ €{price:,.2f} ({units:.6f} units)")
        return mcp_result("\n".join(lines))

    async def get_watchlist(args: dict[str, Any]) -> dict[str, Any]:
        cfg = await _load_asset_config(kv)
        if not cfg:
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
        "get_trade_book": get_trade_book,
        "get_watchlist": get_watchlist,
    }
