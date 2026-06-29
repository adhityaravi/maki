"""Tests for ``maki_common.trading`` — parser + KV-backed book/capital.

The KV tests use a tiny in-memory fake that mirrors the slice of the
``nats.js.kv.KeyValue`` API the trading module touches: ``get`` and
``put``. That's sufficient because the trading module only does
read-modify-write of JSON blobs, not CAS.

Plain ``assert`` and ``try/except`` are used instead of
``pytest.raises`` to keep the type checker happy without listing pytest
as a dev dependency on each subpackage — matches the style of the other
tests in this directory.
"""

from __future__ import annotations

import asyncio
import json
import math

from maki_common.trading import (
    DEFAULT_SEED_EUR,
    AddCash,
    Direction,
    Trade,
    add_cash,
    append_trade,
    compute_position,
    load_book,
    load_seed,
    parse_manual_command,
    parse_trade_command,
    safe_symbol,
)


def _run(coro):
    return asyncio.run(coro)


def _approx(a: float, b: float, *, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── Fake KV ─────────────────────────────────────────────────────────────────


class _FakeEntry:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _FakeKV:
    """Minimal async KV stand-in. Raises KeyError-equivalent on missing keys."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> _FakeEntry:
        if key not in self._store:
            raise KeyError(key)
        return _FakeEntry(self._store[key])

    async def put(self, key: str, value: bytes) -> None:
        self._store[key] = value


# ── safe_symbol ─────────────────────────────────────────────────────────────


def test_safe_symbol_lowercases_and_replaces_separators() -> None:
    assert safe_symbol("BTC") == "btc"
    assert safe_symbol("BTC/EUR") == "btc_eur"
    assert safe_symbol("US 500") == "us_500"


# ── parse_trade_command ─────────────────────────────────────────────────────


def test_parse_buy_with_price_and_note() -> None:
    trade = parse_trade_command("!trade BUY BTC 100 @65234.12 dca round")
    assert trade.direction is Direction.BUY
    assert trade.direction.value == "buy"
    assert trade.direction.name == "BUY"
    assert trade.symbol == "BTC"
    assert trade.amount_eur == 100.0
    assert trade.price == 65234.12
    assert trade.note == "dca round"


def test_parse_sell_without_price() -> None:
    trade = parse_trade_command("!trade SELL ETH 50")
    assert trade.direction is Direction.SELL
    assert trade.symbol == "ETH"
    assert trade.amount_eur == 50.0
    assert trade.price is None
    assert trade.note is None


def test_parse_lowercase_and_comma_decimals() -> None:
    """EU users type commas; symbols may come in lowercase."""
    trade = parse_trade_command("buy sol 25,50 @145,25")
    assert trade.direction is Direction.BUY
    assert trade.symbol == "SOL"
    assert trade.amount_eur is not None and _approx(trade.amount_eur, 25.50)
    assert trade.price is not None and _approx(trade.price, 145.25)


def _expect_value_error(callable_, *args, **kwargs) -> str:
    try:
        callable_(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected ValueError")


def test_parse_rejects_unknown_verb() -> None:
    msg = _expect_value_error(parse_trade_command, "!trade YOLO BTC 100")
    assert "unknown verb" in msg


def test_parse_rejects_missing_amount() -> None:
    _expect_value_error(parse_trade_command, "!trade BUY BTC")


def test_parse_rejects_negative_amount() -> None:
    msg = _expect_value_error(parse_trade_command, "!trade BUY BTC -5")
    assert "amount" in msg


def test_parse_rejects_non_numeric_price() -> None:
    msg = _expect_value_error(parse_trade_command, "!trade BUY BTC 100 @notaprice")
    assert "price" in msg


def test_parse_empty_command() -> None:
    _expect_value_error(parse_trade_command, "")


# ── parse_manual_command (ADDCASH + dispatch to BUY/SELL) ───────────────────


def test_parse_manual_addcash_basic() -> None:
    cmd = parse_manual_command("!trade ADDCASH 200")
    assert isinstance(cmd, AddCash)
    assert cmd.amount_eur == 200.0
    assert cmd.note is None


def test_parse_manual_addcash_with_note_and_comma_decimal() -> None:
    cmd = parse_manual_command("!trade addcash 12,50 bonus payout")
    assert isinstance(cmd, AddCash)
    assert _approx(cmd.amount_eur, 12.50)
    assert cmd.note == "bonus payout"


def test_parse_manual_addcash_rejects_missing_amount() -> None:
    msg = _expect_value_error(parse_manual_command, "!trade ADDCASH")
    assert "ADDCASH" in msg and "amount" in msg


def test_parse_manual_addcash_rejects_non_positive() -> None:
    _expect_value_error(parse_manual_command, "!trade ADDCASH -5")
    _expect_value_error(parse_manual_command, "!trade ADDCASH 0")


def test_parse_manual_addcash_rejects_non_numeric() -> None:
    msg = _expect_value_error(parse_manual_command, "!trade ADDCASH lots")
    assert "amount" in msg


def test_parse_manual_dispatches_to_trade_on_buy() -> None:
    cmd = parse_manual_command("!trade BUY BTC 100 @65234.12")
    assert isinstance(cmd, Trade)
    assert cmd.direction is Direction.BUY
    assert cmd.symbol == "BTC"
    assert cmd.amount_eur == 100.0
    assert cmd.price == 65234.12


def test_parse_manual_empty_rejected() -> None:
    _expect_value_error(parse_manual_command, "")
    _expect_value_error(parse_manual_command, "   ")


def test_parse_manual_unknown_verb_rejected() -> None:
    msg = _expect_value_error(parse_manual_command, "!trade YOLO BTC 100")
    assert "unknown verb" in msg


# ── append_trade / load_book ────────────────────────────────────────────────


def test_append_trade_persists_entry() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        await append_trade(kv, "BTC", direction="buy", price=60000.0, size_eur=100.0)
        entries = await load_book(kv, "BTC")
        assert len(entries) == 1
        assert entries[0]["direction"] == "buy"
        assert entries[0]["price"] == 60000.0
        assert entries[0]["size_eur"] == 100.0
        assert "timestamp" in entries[0]

    _run(scenario())


def test_append_trade_appends_to_existing_book() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        await append_trade(kv, "BTC", direction="buy", price=60000.0, size_eur=100.0)
        await append_trade(kv, "BTC", direction="sell", price=70000.0, size_eur=70.0)
        entries = await load_book(kv, "BTC")
        assert [e["direction"] for e in entries] == ["buy", "sell"]

    _run(scenario())


def test_append_trade_rejects_invalid_direction() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        try:
            await append_trade(kv, "BTC", direction="hodl", price=1.0, size_eur=1.0)
        except ValueError:
            return
        raise AssertionError("expected ValueError")

    _run(scenario())


def test_append_trade_rejects_non_positive_price() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        try:
            await append_trade(kv, "BTC", direction="buy", price=0, size_eur=1.0)
        except ValueError:
            return
        raise AssertionError("expected ValueError")

    _run(scenario())


def test_load_book_handles_corrupted_value() -> None:
    """A non-list JSON blob should degrade to empty rather than crash."""

    async def scenario() -> None:
        kv = _FakeKV()
        await kv.put("trading.book.btc", json.dumps({"oops": True}).encode())
        assert await load_book(kv, "BTC") == []

    _run(scenario())


# ── compute_position ────────────────────────────────────────────────────────


def test_compute_position_average_cost_and_realized_pnl() -> None:
    entries = [
        {"direction": "buy", "price": 100.0, "size_eur": 100.0},  # 1 unit @ 100
        {"direction": "buy", "price": 200.0, "size_eur": 200.0},  # 1 unit @ 200, avg cost = 150
        {"direction": "sell", "price": 250.0, "size_eur": 250.0},  # sell 1 unit @ 250
    ]
    pos = compute_position("BTC", entries)
    assert pos["symbol"] == "BTC"
    assert _approx(pos["net_units"], 1.0)
    assert _approx(pos["avg_cost"], 150.0)
    assert _approx(pos["realized_pnl"], 100.0)  # (250 - 150) * 1 unit
    assert pos["is_open"] is True


def test_compute_position_skips_zero_price_entries() -> None:
    entries = [
        {"direction": "buy", "price": 0.0, "size_eur": 100.0},  # ignored
        {"direction": "buy", "price": 100.0, "size_eur": 100.0},
    ]
    pos = compute_position("BTC", entries)
    assert _approx(pos["net_units"], 1.0)
    assert _approx(pos["avg_cost"], 100.0)


def test_compute_position_flat_when_fully_sold() -> None:
    entries = [
        {"direction": "buy", "price": 100.0, "size_eur": 100.0},
        {"direction": "sell", "price": 100.0, "size_eur": 100.0},
    ]
    pos = compute_position("BTC", entries)
    assert _approx(pos["net_units"], 0.0)
    assert pos["is_open"] is False


# ── capital ─────────────────────────────────────────────────────────────────


def test_load_seed_returns_default_when_missing() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        assert await load_seed(kv) == DEFAULT_SEED_EUR

    _run(scenario())


def test_add_cash_grows_seed_and_persists() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        new_seed = await add_cash(kv, 50.0)
        assert new_seed == DEFAULT_SEED_EUR + 50.0
        # Second add reads the persisted value, not the default
        new_seed2 = await add_cash(kv, 25.0)
        assert new_seed2 == DEFAULT_SEED_EUR + 75.0
        assert await load_seed(kv) == DEFAULT_SEED_EUR + 75.0

    _run(scenario())


def test_add_cash_rejects_non_positive() -> None:
    async def scenario() -> None:
        kv = _FakeKV()
        for bad in (0, -5):
            try:
                await add_cash(kv, bad)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {bad}")

    _run(scenario())
