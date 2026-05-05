"""Trade book — KV-backed list of fills per symbol.

Each book is stored at ``trading.book.{safe_symbol(symbol)}`` as a JSON
list of entries::

    {"timestamp": iso8601, "direction": "buy"|"sell", "price": float, "size_eur": float}

Position math uses average-cost basis. Realised P&L is computed against
the running average cost at the time of each sell. ``net_units`` is
clamped at zero so over-selling (selling more than was bought) doesn't
go negative — mirroring the original ``maki_loops`` semantics that the
``trading_portfolio`` tools were written against.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# Public KV key conventions — read by maki_common.tools.trading_portfolio
# and written here. Treat as part of the public API of this module.
KV_BOOK_PREFIX = "trading.book"


def safe_symbol(symbol: str) -> str:
    """Normalise a symbol for use as a KV key suffix.

    Lower-cased, slashes and spaces replaced with underscores so e.g.
    ``BTC/EUR`` becomes ``btc_eur``. Stable across the project — both
    the writer (here) and readers (``trading_portfolio``) use this.
    """
    return symbol.lower().replace("/", "_").replace(" ", "_")


async def load_book(kv: Any, symbol: str) -> list[dict]:
    """Return raw trade entries for *symbol*, or ``[]`` on miss."""
    try:
        entry = await kv.get(f"{KV_BOOK_PREFIX}.{safe_symbol(symbol)}")
        raw = json.loads(entry.value.decode())
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


async def append_trade(
    kv: Any,
    symbol: str,
    *,
    direction: str,
    price: float,
    size_eur: float,
    timestamp: str | None = None,
) -> list[dict]:
    """Append a fill to the trade book for *symbol*.

    Reads the current list (or starts empty), appends one entry, writes
    back. Not atomic across writers — fine for the manual ``!trade``
    command path which is single-pod via the STEM_QUEUE group, but don't
    call this from multiple pods concurrently for the same symbol.

    Args:
        kv: NATS KV bucket (the ``maki-lock`` bucket).
        symbol: Asset symbol (case-insensitive — normalised with ``safe_symbol``).
        direction: ``"buy"`` or ``"sell"``.
        price: Fill price in EUR.
        size_eur: Notional size in EUR.
        timestamp: Optional ISO-8601 timestamp; defaults to ``now(UTC)``.

    Returns:
        The full updated list of entries (handy for callers that want
        the new length without re-reading).
    """
    if direction not in ("buy", "sell"):
        raise ValueError(f"direction must be 'buy' or 'sell', got {direction!r}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    if size_eur <= 0:
        raise ValueError(f"size_eur must be positive, got {size_eur}")

    entries = await load_book(kv, symbol)
    entries.append(
        {
            "timestamp": timestamp or datetime.now(UTC).isoformat(),
            "direction": direction,
            "price": float(price),
            "size_eur": float(size_eur),
        }
    )
    await kv.put(
        f"{KV_BOOK_PREFIX}.{safe_symbol(symbol)}",
        json.dumps(entries).encode(),
    )
    return entries


def compute_position(symbol: str, entries: list[dict]) -> dict:
    """Average-cost basis position from a list of book entries.

    Returns a plain dict (not a dataclass) so it round-trips through JSON
    cleanly when surfaced by tools. Mirrors the math in
    ``maki_common.tools.trading_portfolio._compute_position`` — kept here
    as the canonical owner so the tools module can delegate.
    """
    total_units_bought = 0.0
    total_units_sold = 0.0
    total_bought_eur = 0.0
    total_sold_eur = 0.0

    for e in entries:
        price = float(e.get("price", 0) or 0)
        size_eur = float(e.get("size_eur", 0) or 0)
        if price <= 0:
            continue
        units = size_eur / price
        direction = e.get("direction", "")
        if direction == "buy":
            total_units_bought += units
            total_bought_eur += size_eur
        elif direction == "sell":
            total_units_sold += units
            total_sold_eur += size_eur

    avg_cost = total_bought_eur / total_units_bought if total_units_bought > 0 else 0.0

    realized_pnl = 0.0
    for e in entries:
        if e.get("direction") != "sell":
            continue
        price = float(e.get("price", 0) or 0)
        if price <= 0:
            continue
        sell_units = float(e.get("size_eur", 0) or 0) / price
        realized_pnl += (price - avg_cost) * sell_units

    net_units = max(0.0, total_units_bought - total_units_sold)
    return {
        "symbol": symbol,
        "net_units": net_units,
        "avg_cost": avg_cost,
        "total_bought_eur": total_bought_eur,
        "total_sold_eur": total_sold_eur,
        "realized_pnl": realized_pnl,
        "is_open": net_units > 1e-12,
        "entries": entries,
    }
