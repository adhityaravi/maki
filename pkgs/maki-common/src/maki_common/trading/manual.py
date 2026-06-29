"""Parser for the Discord ``!trade`` command (BUY/SELL and ADDCASH).

Single source of truth for ``!trade`` parsing — used by both ears (for
immediate Discord feedback on the user's input) and stem (which consumes
the structured payload ears publishes to NATS). Before this lived here,
the two services hand-rolled tokenization independently and drifted.

Grammar::

    !trade {BUY|SELL} {SYMBOL} {AMOUNT_EUR} [@PRICE] [NOTE...]
    !trade ADDCASH {AMOUNT_EUR} [NOTE...]

Examples::

    !trade BUY BTC 100 @65234.12 dca
    !trade SELL ETH 50
    !trade buy sol 25 @145
    !trade ADDCASH 200 bonus

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


@dataclass(frozen=True)
class AddCash:
    """A parsed ``!trade ADDCASH`` command — grow the seed by ``amount_eur``."""

    amount_eur: float
    note: str | None = None


# Tagged union of all things ``!trade`` can mean. Use ``isinstance`` to
# discriminate at call sites — both variants are frozen dataclasses, so
# the type checker narrows cleanly.
ManualCommand = AddCash | Trade


def _parse_number(token: str, *, label: str) -> float:
    """Parse a positive float, allowing comma decimal separators."""
    try:
        value = float(token.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, got `{token}`") from exc
    if not (value > 0):  # also catches NaN
        raise ValueError(f"{label} must be positive, got `{token}`")
    return value


def _strip_leading_trade(tokens: list[str]) -> list[str]:
    """Drop an optional leading ``!trade`` / ``trade`` so callers don't pre-trim."""
    if tokens and tokens[0].lower() in ("!trade", "trade"):
        return tokens[1:]
    return tokens


def parse_manual_command(content: str) -> ManualCommand:
    """Parse any ``!trade`` command — ADDCASH or BUY/SELL — into a tagged union.

    Returns :class:`AddCash` for ``!trade ADDCASH AMOUNT [NOTE...]`` and
    :class:`Trade` for ``!trade BUY|SELL SYMBOL AMOUNT [@PRICE] [NOTE...]``.

    Raises :class:`ValueError` with a user-friendly message on any
    malformed input — both ears and stem surface that message to Discord
    unchanged, so wording stays consistent across the two services.
    """
    tokens = _strip_leading_trade((content or "").strip().split())
    if not tokens:
        raise ValueError("empty command")

    verb = tokens[0].upper()

    if verb == "ADDCASH":
        if len(tokens) < 2:
            raise ValueError("ADDCASH: missing amount")
        amount_eur = _parse_number(tokens[1], label="amount")
        note = " ".join(tokens[2:]).strip() or None
        return AddCash(amount_eur=amount_eur, note=note)

    # Delegate BUY/SELL to the dedicated parser so error wording stays in
    # one place. ``parse_trade_command`` also tolerates the leading
    # ``!trade``; we've already stripped it, which is fine — the parser
    # re-strips defensively.
    return parse_trade_command(" ".join(tokens))


def parse_trade_command(command: str) -> Trade:
    """Parse a ``!trade BUY/SELL`` command string into a :class:`Trade`.

    Accepts the leading ``!trade`` token (extra tolerant — also accepts
    a bare ``BUY ...``) so the listener can pass through whatever ears
    forwarded without trimming.

    Raises ``ValueError`` with a user-friendly message on any malformed
    input — the listener surfaces the message to Discord.
    """
    tokens = _strip_leading_trade((command or "").strip().split())
    if not tokens:
        raise ValueError("empty command")

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
