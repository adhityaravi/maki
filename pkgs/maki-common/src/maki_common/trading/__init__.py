"""Trading primitives shared across maki services.

The previous owner of this code, ``maki_loops.trading``, was never wired up
in this monorepo — referenced in mypy's ``ignore_missing_imports`` but
never installed. Stem's ``trading_manual_listener`` imported from it and
crashed silently on first message (issue #166). The logic is small (KV
read/write + a parser), so it lives here as a sibling of
``maki_common.tools.trading_portfolio`` which already mirrored several of
these helpers.

KV layout (bucket = ``maki-lock``):

* ``trading.book.{symbol_safe}``  — list of trade entries
* ``trading.capital``             — ``{"seed_eur": float}``
* ``trading.asset_config``        — ``{"crypto": [...], "eu_stocks": [...], "us_stocks": [...]}``
* ``trading.price.{symbol_safe}`` — ``{"price": float, "timestamp": iso}``
"""

from __future__ import annotations

from maki_common.trading.book import (
    KV_BOOK_PREFIX,
    append_trade,
    compute_position,
    load_book,
    safe_symbol,
)
from maki_common.trading.capital import (
    DEFAULT_SEED_EUR,
    KV_CAPITAL,
    add_cash,
    load_seed,
)
from maki_common.trading.manual import (
    AddCash,
    Direction,
    ManualCommand,
    Trade,
    parse_manual_command,
    parse_trade_command,
)

__all__ = [
    "DEFAULT_SEED_EUR",
    "AddCash",
    "Direction",
    "KV_BOOK_PREFIX",
    "KV_CAPITAL",
    "ManualCommand",
    "Trade",
    "add_cash",
    "append_trade",
    "compute_position",
    "load_book",
    "load_seed",
    "parse_manual_command",
    "parse_trade_command",
    "safe_symbol",
]
