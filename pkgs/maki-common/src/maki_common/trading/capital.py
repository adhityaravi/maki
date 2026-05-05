"""Capital seed — the EUR pot the trading loop sizes against.

Stored at ``trading.capital`` in the ``maki-lock`` KV bucket as
``{"seed_eur": float}``. The default of €200 matches the value the
original ``maki_loops.trading.capital`` module shipped with — kept here
so behaviour is unchanged for live deployments that have never had the
key written.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

KV_CAPITAL = "trading.capital"
DEFAULT_SEED_EUR = 200.0


async def load_seed(kv: Any) -> float:
    """Return the current capital seed in EUR, falling back to the default."""
    try:
        entry = await kv.get(KV_CAPITAL)
        data = json.loads(entry.value.decode())
        return float(data.get("seed_eur", DEFAULT_SEED_EUR))
    except Exception:
        return DEFAULT_SEED_EUR


async def add_cash(kv: Any, amount_eur: float) -> float:
    """Grow the seed by *amount_eur* and return the new total.

    Raises ``ValueError`` for non-positive or NaN amounts so the caller
    can surface the error to Discord without crashing the listener.
    """
    if not (amount_eur > 0):  # also catches NaN
        raise ValueError(f"amount must be positive, got {amount_eur}")

    current = await load_seed(kv)
    new_seed = current + float(amount_eur)
    await kv.put(KV_CAPITAL, json.dumps({"seed_eur": new_seed}).encode())
    return new_seed
