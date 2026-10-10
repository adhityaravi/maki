"""Trading-specific NATS listeners for maki-stem.

Keeps ``!trade`` command handling and cortex tool dispatch out of
``main.py``. Dependencies (NATS connection, KV handles, tool
registries) are passed in by the caller rather than pulled from module
globals, mirroring ``maki_ears.trading``.

Historical note: this module used to also host ``trading_signal_listener``,
which persisted accepted trade signals from the automated trading loop into
the ``trade_signals`` PostgreSQL table. That loop lived in the now-deleted
``maki_loops`` repo, leaving the listener with zero publishers of
``TRADING_SIGNAL`` — it was removed in #168. The ``trade_signals`` table
is kept for historical rows; the broader excise-vs-revive question for
the remainder of the trading subsystem is tracked in #242.
"""

from __future__ import annotations

import json
import logging

from maki_common import subscribe_supervised
from maki_common.subjects import (
    EARS_OUT,
    TRADING_MANUAL_TRADE,
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


# ── !trade command dispatch ──────────────────────────────────────────────────


async def trading_manual_listener(nc, lock_kv) -> None:
    """Handle ``!trade`` commands from ears.

    Ears parses with :func:`maki_common.trading.parse_manual_command` and
    publishes a structured payload (issue #116) — this listener consumes
    that payload directly and never re-tokenizes the raw command string,
    so error wording and accepted syntax stay in lock-step across both
    services.

    ``kind == "addcash"`` grows the seed via
    :func:`maki_common.trading.add_cash`; ``kind == "trade"`` is appended
    to the trade book via :func:`maki_common.trading.append_trade`. Acks
    are published back to EARS_OUT so Discord can display them.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent !trade
    command (issue #175).
    """
    from maki_common.trading import add_cash, append_trade

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
            kind = data.get("kind")

            if kind == "addcash":
                amount = float(data.get("amount_eur") or 0)
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

            if kind == "trade":
                symbol = str(data.get("symbol") or "").upper()
                direction = str(data.get("direction") or "").lower()
                amount_eur = float(data.get("amount_eur") or 0)
                raw_price = data.get("price")
                price: float | None = float(raw_price) if raw_price is not None else None
                verb_name = direction.upper()

                if price is None:
                    await _ack(f"⚠️ {verb_name} {symbol} logged without price — not appended to book (price required)")
                    return

                await append_trade(
                    lock_kv,
                    symbol,
                    direction=direction,
                    price=price,
                    size_eur=amount_eur,
                )
                log.info(
                    "Manual trade appended to book",
                    extra={
                        "symbol": symbol,
                        "direction": direction,
                        "amount_eur": amount_eur,
                        "price": price,
                    },
                )
                emoji = "🟢" if direction == "buy" else "🔴"
                await _ack(f"{emoji} **{verb_name}** {symbol} €{amount_eur:.2f} @ €{price:.2f} — booked")
                return

            log.warning("Unknown manual-trade kind", extra={"kind": kind})
        except Exception:
            log.exception("Failed to process manual trade command")

    await subscribe_supervised(
        nc,
        TRADING_MANUAL_TRADE,
        _handle,
        queue=STEM_QUEUE,
        # Broker roundtrip. Sixty seconds is generous but bounds hung
        # broker connections so subsequent !trade commands aren't lost
        # on this pod (#492).
        handler_timeout=60.0,
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
        # Cortex-facing tool dispatch. Sixty seconds covers slow broker
        # calls (quotes, positions) without letting a wedged tool freeze
        # every subsequent cortex tool call on this pod (#492).
        handler_timeout=60.0,
        name="stem.trading_tool",
    )
