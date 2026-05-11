"""Trading-specific NATS listeners for maki-stem.

Keeps trade signal persistence, ``!trade`` command handling, and cortex
tool dispatch out of ``main.py``. Dependencies (NATS connection, DB pool,
KV handles, tool registries) are passed in by the caller rather than
pulled from module globals, mirroring ``maki_ears.trading``.
"""

from __future__ import annotations

import json
import logging

from maki_common import subscribe_supervised
from maki_common.subjects import (
    EARS_OUT,
    TRADING_MANUAL_TRADE,
    TRADING_SIGNAL,
    TRADING_TOOL_REQUEST,
)

log = logging.getLogger(__name__)

# Single source of truth for the NATS queue group shared across all stem pods.
# Keep in sync with ``maki_stem.main.STEM_QUEUE``. Every write-side or
# request/reply listener MUST subscribe with this queue so a rolling deploy
# (where two pods coexist briefly) doesn't double-write or double-handle a
# request. Broadcast/fan-out listeners intentionally omit it — see the
# comment at each subscribe site.
STEM_QUEUE = "maki-stem"


# ── Trade signal persistence ─────────────────────────────────────────────────


async def trading_signal_listener(nc, db_pool) -> None:
    """Persist accepted trade signals to maki-vault (PostgreSQL).

    Subscribes to TRADING_SIGNAL, published by the trading loop after a
    proposal is accepted. Inserts into the ``trade_signals`` table.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent accepted
    trade (issue #175).
    """

    async def _handle(msg) -> None:
        try:
            data = json.loads(msg.data.decode())

            # Map direction: buy→long, sell→short
            direction = data.get("direction", "")
            db_direction = "long" if direction == "buy" else "short"

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trade_signals (
                        trade_id, asset, asset_type, direction, entry_price,
                        position_size_pct, composite_score,
                        sentiment_score, indicator_snapshot,
                        paper, status, accepted_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'accepted', now())
                    """,
                    data.get("trade_id"),
                    data.get("asset"),
                    data.get("asset_type", "crypto"),
                    db_direction,
                    float(data.get("entry_price", 0)),
                    float(data.get("position_size_eur", 0) or data.get("position_size_pct", 0)),
                    float(data.get("composite_score", 0)),
                    float(data.get("sentiment_score", 0)) if data.get("sentiment_score") is not None else None,
                    json.dumps(data.get("indicator_snapshot")) if data.get("indicator_snapshot") else None,
                    data.get("paper", True),
                )
            log.info(
                "Trade signal persisted",
                extra={"trade_id": data.get("trade_id"), "asset": data.get("asset")},
            )
        except Exception:
            log.exception("Failed to persist trade signal")

    await subscribe_supervised(
        nc,
        TRADING_SIGNAL,
        _handle,
        queue=STEM_QUEUE,
        name="stem.trading_signal",
    )


# ── !trade command dispatch ──────────────────────────────────────────────────


async def trading_manual_listener(nc, lock_kv) -> None:
    """Handle ``!trade`` commands from ears.

    ADDCASH grows the seed via :func:`maki_common.trading.capital.add_cash`;
    BUY/SELL are parsed and appended to the trade book via
    :func:`maki_common.trading.book.append_trade`. Acks are published back
    to EARS_OUT so Discord can display them.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent !trade
    command (issue #175).
    """
    from maki_common.trading import (
        add_cash,
        append_trade,
        parse_trade_command,
    )

    async def _ack(text: str) -> None:
        try:
            await nc.publish(
                EARS_OUT,
                json.dumps(
                    {
                        "text": text,
                        "turn_id": "trading-manual",
                        "channel": "trading",
                    }
                ).encode(),
            )
        except Exception:
            log.warning("Failed to publish manual-trade ack", exc_info=True)

    async def _handle(msg) -> None:
        try:
            data = json.loads(msg.data.decode())
            command = (data.get("command") or "").strip()
            tokens = command.split()
            if len(tokens) < 2:
                return
            verb = tokens[1].upper()

            if verb == "ADDCASH":
                if len(tokens) < 3:
                    await _ack("❌ ADDCASH: missing amount")
                    return
                try:
                    amount = float(tokens[2])
                except ValueError:
                    await _ack(f"❌ ADDCASH: invalid amount `{tokens[2]}`")
                    return
                try:
                    new_seed = await add_cash(lock_kv, amount)
                except ValueError as exc:
                    await _ack(f"❌ ADDCASH: {exc}")
                    return
                log.info(
                    "Capital added",
                    extra={"amount_eur": amount, "new_seed": new_seed},
                )
                await _ack(f"💰 +€{amount:.2f} — seed now €{new_seed:.2f}")
                return

            # BUY / SELL — parse via manual module, append to book if priced
            try:
                trade = parse_trade_command(command)
            except ValueError as exc:
                await _ack(f"❌ {exc}")
                return

            if trade.price is None:
                await _ack(
                    f"⚠️ {trade.direction.name} {trade.symbol} logged without price — "
                    "not appended to book (price required)"
                )
                return

            await append_trade(
                lock_kv,
                trade.symbol,
                direction=trade.direction.value,
                price=trade.price,
                size_eur=trade.amount_eur,
            )
            log.info(
                "Manual trade appended to book",
                extra={
                    "symbol": trade.symbol,
                    "direction": trade.direction.value,
                    "amount_eur": trade.amount_eur,
                    "price": trade.price,
                },
            )
            emoji = "🟢" if trade.direction.name == "BUY" else "🔴"
            await _ack(
                f"{emoji} **{trade.direction.name}** {trade.symbol} "
                f"€{trade.amount_eur:.2f} @ €{trade.price:.2f} — booked"
            )
        except Exception:
            log.exception("Failed to process manual trade command")

    await subscribe_supervised(
        nc,
        TRADING_MANUAL_TRADE,
        _handle,
        queue=STEM_QUEUE,
        name="stem.trading_manual",
    )


# ── Cortex tool dispatch ─────────────────────────────────────────────────────


async def trading_tool_listener(nc, tool_registry: dict, permanent_tools: dict) -> None:
    """Handle trading tool requests from cortex via NATS request/reply.

    Priority: loop-specific registry (live market context) over permanent
    tools (read-only KV). Both dicts are passed by reference so that the
    trading loop can mutate ``tool_registry`` mid-run to publish
    context-scoped tools.

    A tool request fans out to every stem pod in the mesh; only the pod
    that actually has the handler responds. Pods without the tool stay
    silent so the requester doesn't see a fast "unknown tool" error from
    one pod win the race against the real answer from another. If no pod
    has the tool, the requester's NATS request times out — that's fine
    and correct.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent tool
    request (issue #175). Intentional fan-out: every pod must see the
    request so the one holding the handler can answer — no queue group.
    """

    async def _handle(msg) -> None:
        data: dict = {}
        try:
            data = json.loads(msg.data.decode())
            tool_name = data.get("tool_name", "")
            tool_args = data.get("tool_args", {})
            handler = tool_registry.get(tool_name) or permanent_tools.get(tool_name)
            if handler is None:
                return  # not our tool — another pod will answer, or timeout
            result = await handler(tool_args)
            await msg.respond(json.dumps(result).encode())
        except Exception:
            log.exception("Trading tool error", extra={"tool_name": data.get("tool_name", "?")})
            try:
                from maki_common.tools.utils import mcp_result

                await msg.respond(json.dumps(mcp_result("Internal tool error")).encode())
            except Exception:
                pass

    await subscribe_supervised(
        nc,
        TRADING_TOOL_REQUEST,
        _handle,
        name="stem.trading_tool",
    )
