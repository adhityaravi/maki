"""Trading-specific Discord handlers for maki-ears.

Keeps the trading UI (proposal cards, button clicks) and the ``!trade``
command handling out of ``main.py``. Pure functions — the NATS connection
and dedup KV are passed in by the caller rather than pulled from module
globals.
"""

from __future__ import annotations

import json
import logging

import discord
from maki_common.subjects import EARS_INTERACTION, TRADING_MANUAL_TRADE
from maki_common.trading import (
    AddCash,
    Direction,
    Trade,
    parse_manual_command,
)

log = logging.getLogger(__name__)


# ── UI ───────────────────────────────────────────────────────────────────────


def _format_price(price: float) -> str:
    """Format a price with up to 8 decimals, stripped of trailing zeros.

    Used both for the button custom_id (encoding the expected price so it
    survives ears restarts) and for the modal's prefilled default.
    """
    if not price or price <= 0:
        return ""
    # 8 decimals covers sub-cent crypto (e.g. shitcoins) without scientific
    # notation. We strip trailing zeros so "65234.120000" becomes "65234.12".
    s = f"{price:.8f}".rstrip("0").rstrip(".")
    return s or "0"


class TradeProposalView(discord.ui.View):
    """Button view attached to a trade proposal message.

    ``timeout=None`` keeps buttons alive for the full 60-min collection
    window. Interaction handling lives in :func:`handle_trade_interaction`
    (not in button callbacks) so clicks still work after ears restarts —
    Discord resends the interaction regardless of in-memory view state.

    The expected fill price is baked into the accept button's ``custom_id``
    so the price modal can prefill it even after an ears restart (no
    in-memory state survives).
    """

    def __init__(
        self,
        proposal_id: str,
        symbol: str,
        direction: str = "buy",
        entry_price: float = 0.0,
    ) -> None:
        super().__init__(timeout=None)
        price_str = _format_price(entry_price)
        if direction == "buy":
            label, style = "🟢 Buy", discord.ButtonStyle.success
        else:
            label, style = "🔴 Sell", discord.ButtonStyle.danger
        self.add_item(
            discord.ui.Button(
                label=label,
                custom_id=f"trade:{proposal_id}:{symbol}:accept:{price_str}",
                style=style,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="⏭️ Skip",
                custom_id=f"trade:{proposal_id}:{symbol}:skip",
                style=discord.ButtonStyle.secondary,
            )
        )


class TradePriceModal(discord.ui.Modal):
    """Modal shown after the accept button — asks for the real fill price.

    The expected price (from the analyst's signal) is used as the
    ``default`` of the text input. Submitting without edits books the
    trade at the expected price.
    """

    def __init__(self, proposal_id: str, symbol: str, direction: str, expected_price: float) -> None:
        verb = "Buy" if direction == "buy" else "Sell"
        title = f"{verb} {symbol} — fill price"[:45]  # Discord modal title cap
        super().__init__(title=title, custom_id=f"trade:{proposal_id}:{symbol}:fill")
        self.price_input = discord.ui.TextInput(
            label="Actual fill price",
            placeholder="Price you filled at (same currency as card)",
            default=_format_price(expected_price),
            required=True,
            max_length=32,
        )
        self.add_item(self.price_input)


# ── Interaction routing ──────────────────────────────────────────────────────


async def _publish_outcome(
    nc,
    proposal_id: str,
    action: str,
    user: str,
    actual_price: float | None,
) -> None:
    """Publish a trade outcome to the proposal's NATS subject."""
    subject = f"{EARS_INTERACTION}.{proposal_id}"
    payload: dict = {
        "proposal_id": proposal_id,
        "action": action,
        "user": user,
    }
    if actual_price is not None:
        payload["actual_price"] = actual_price
    await nc.publish(subject, json.dumps(payload).encode())
    log.info(
        "Trade interaction published",
        extra={"proposal_id": proposal_id, "action": action, "actual_price": actual_price},
    )


async def _edit_proposal_message(interaction: discord.Interaction, suffix: str) -> None:
    """Append a status suffix to the original proposal message and drop buttons."""
    msg = interaction.message
    if msg is None:
        return
    try:
        await msg.edit(content=msg.content + f"\n\n{suffix}", view=None)
    except Exception:
        log.warning("Failed to edit proposal message", exc_info=True)


async def _handle_component(interaction: discord.Interaction, nc) -> bool:
    """Handle a button click on a trade proposal card."""
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("trade:"):
        return False

    parts = custom_id.split(":")
    # Valid shapes: trade:pid:sym:skip  or  trade:pid:sym:accept:price
    if len(parts) < 4 or len(parts) > 5:
        return True  # consumed but malformed — swallow it

    action = parts[3]
    proposal_id = parts[1]
    symbol = parts[2]

    if action == "skip":
        await interaction.response.defer()
        await _edit_proposal_message(interaction, f"_⏭️ Skipped by {interaction.user.name}_")
        if nc is not None:
            await _publish_outcome(nc, proposal_id, "skip", str(interaction.user.name), None)
        return True

    if action == "accept":
        # Parse expected price out of custom_id for the modal prefill
        expected_price = 0.0
        if len(parts) == 5:
            try:
                expected_price = float(parts[4])
            except ValueError:
                expected_price = 0.0
        # Infer direction from the button style isn't trivial; fall back
        # to "buy" for the modal title — the publisher doesn't need it.
        direction = "buy"  # modal title hint only
        modal = TradePriceModal(proposal_id, symbol, direction, expected_price)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            log.exception("Failed to send price modal", extra={"proposal_id": proposal_id})
        return True

    # Unknown action — consume silently
    return True


async def _handle_modal_submit(interaction: discord.Interaction, nc) -> bool:
    """Handle submission of the fill-price modal."""
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("trade:") or not custom_id.endswith(":fill"):
        return False

    parts = custom_id.split(":")
    if len(parts) != 4:
        return True  # consumed but malformed
    proposal_id = parts[1]

    # Extract the price value from the modal's text input
    raw_value = ""
    for component in (interaction.data or {}).get("components", []):
        for child in component.get("components", []):
            if child.get("type") == 4:  # TextInput
                raw_value = (child.get("value") or "").strip()
                break
        if raw_value:
            break

    try:
        actual_price = float(raw_value.replace(",", "."))
        if actual_price <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            f"❌ Invalid price `{raw_value}` — trade not booked. Click Buy/Sell again.",
            ephemeral=True,
        )
        return True

    await interaction.response.defer()
    await _edit_proposal_message(
        interaction,
        f"_✅ Accepted by {interaction.user.name} @ `{actual_price}`_",
    )
    if nc is not None:
        await _publish_outcome(nc, proposal_id, "accept", str(interaction.user.name), actual_price)
    return True


async def handle_trade_interaction(interaction: discord.Interaction, nc) -> bool:
    """Route a Discord trade interaction (button click or modal submit).

    Returns ``True`` if this interaction was consumed, ``False`` if it's
    not for us and the caller should try another handler.
    """
    if interaction.type == discord.InteractionType.component:
        return await _handle_component(interaction, nc)
    if interaction.type == discord.InteractionType.modal_submit:
        return await _handle_modal_submit(interaction, nc)
    return False


# ── !trade command ───────────────────────────────────────────────────────────


_USAGE = "Usage: `!trade {BUY|SELL} {SYMBOL} {AMOUNT_EUR} [@PRICE] [NOTE]`\n       `!trade ADDCASH {AMOUNT_EUR} [NOTE]`"


async def handle_trade_command(
    message: discord.Message,
    content: str,
    nc,
    dedup_kv,
) -> None:
    """Validate, dedup, and forward a ``!trade`` command to stem.

    Formats::

        !trade {BUY|SELL} {SYMBOL} {AMOUNT_EUR} [@PRICE] [NOTE...]
        !trade ADDCASH {AMOUNT_EUR} [NOTE...]

    Parsing is delegated to :func:`maki_common.trading.parse_manual_command`
    — the same parser stem uses to validate. Ears publishes the parsed,
    structured payload to NATS so stem never re-tokenizes the raw string;
    that keeps error wording and accepted syntax in lock-step across both
    services (see issue #116).
    """
    msg_key = f"msg.{message.id}"
    try:
        await dedup_kv.create(msg_key, b"1")
    except Exception:
        log.info("Dedup: trade command already claimed", extra={"message_id": str(message.id)})
        return

    try:
        parsed = parse_manual_command(content)
    except ValueError as exc:
        await message.channel.send(f"❌ {exc}\n{_USAGE}")
        return

    base_payload: dict = {
        "username": message.author.name,
        "message_id": str(message.id),
    }

    # ── ADDCASH — grow the seed ──────────────────────────────────────────
    if isinstance(parsed, AddCash):
        payload = {
            **base_payload,
            "kind": "addcash",
            "amount_eur": parsed.amount_eur,
            "note": parsed.note,
        }
        try:
            await nc.publish(TRADING_MANUAL_TRADE, json.dumps(payload).encode())
            log.info("Addcash command published", extra={"amount_eur": parsed.amount_eur})
        except Exception:
            log.exception("Failed to publish addcash command")
            await message.channel.send("❌ Failed to record — NATS unavailable.")
            return
        await message.channel.send(f"💰 **ADDCASH** €{parsed.amount_eur:.2f} — applying...")
        return

    # ── BUY / SELL — log a trade to the book ─────────────────────────────
    trade: Trade = parsed
    payload = {
        **base_payload,
        "kind": "trade",
        "symbol": trade.symbol,
        "direction": trade.direction.value,
        "amount_eur": trade.amount_eur,
        "price": trade.price,
        "note": trade.note,
    }
    try:
        await nc.publish(TRADING_MANUAL_TRADE, json.dumps(payload).encode())
        log.info(
            "Trade command published",
            extra={
                "symbol": trade.symbol,
                "direction": trade.direction.value,
                "amount_eur": trade.amount_eur,
            },
        )
    except Exception:
        log.exception("Failed to publish trade command")
        await message.channel.send("❌ Failed to record trade — NATS unavailable.")
        return

    direction_emoji = "🟢" if trade.direction is Direction.BUY else "🔴"
    await message.channel.send(
        f"{direction_emoji} **{trade.direction.name}** {trade.symbol} €{trade.amount_eur:.2f} — logged"
    )
