"""Parser for the Discord ``!trade`` command (BUY/SELL form).

ADDCASH is handled inline by the listener — it's a one-token verb plus
an amount and doesn't need its own parser. BUY/SELL is richer (optional
price, optional free-text note) and benefits from a dedicated parser
that the listener can delegate to.

Grammar::

    !trade {BUY|SELL} {SYMBOL} {AMOUNT_EUR} [@PRICE] [NOTE...]

Examples::

    !trade BUY BTC 100 @65234.12 dca
    !trade SELL ETH 50
    !trade buy sol 25 @145

Symbols are upper-cased to match the watchlist convention. Amounts and
prices accept ``,`` as a decimal separator (EU locales) for usability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Direction(StrEnum):
    """Trade side. ``.value`` matches the on-disk book schema (lowercase)."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Trade:
    """A parsed ``!trade BUY/SELL`` command, ready to append to the book."""

    symbol: str
    direction: Direction
    amount_eur: float
    price: float | None = None
    note: str | None = None


def _parse_number(token: str, *, label: str) -> float:
    """Parse a positive float, allowing comma decimal separators."""
    try:
        value = float(token.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, got `{token}`") from exc
    if not (value > 0):  # also catches NaN
        raise ValueError(f"{label} must be positive, got `{token}`")
    return value


def parse_trade_command(command: str) -> Trade:
    """Parse a full ``!trade BUY/SELL`` command string into a :class:`Trade`.

    Accepts the leading ``!trade`` token (extra tolerant — also accepts
    a bare ``BUY ...``) so the listener can pass through whatever ears
    forwarded without trimming.

    Raises ``ValueError`` with a user-friendly message on any malformed
    input — the listener surfaces the message to Discord.
    """
    tokens = (command or "").strip().split()
    if not tokens:
        raise ValueError("empty command")

    # Strip an optional leading "!trade" so callers don't have to pre-trim.
    if tokens[0].lower() in ("!trade", "trade"):
        tokens = tokens[1:]

    if len(tokens) < 3:
        raise ValueError("usage: BUY|SELL SYMBOL AMOUNT_EUR [@PRICE] [NOTE...]")

    verb = tokens[0].upper()
    if verb not in ("BUY", "SELL"):
        raise ValueError(f"unknown verb `{tokens[0]}` (expected BUY or SELL)")
    direction = Direction.BUY if verb == "BUY" else Direction.SELL

    symbol = tokens[1].upper()
    if not symbol:
        raise ValueError("symbol is required")

    amount_eur = _parse_number(tokens[2], label="amount")

    price: float | None = None
    note_tokens: list[str] = []
    for tok in tokens[3:]:
        if price is None and tok.startswith("@") and len(tok) > 1:
            price = _parse_number(tok[1:], label="price")
        else:
            note_tokens.append(tok)

    note = " ".join(note_tokens).strip() or None
    return Trade(
        symbol=symbol,
        direction=direction,
        amount_eur=amount_eur,
        price=price,
        note=note,
    )
