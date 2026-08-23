"""maki-ears: Discord interface for Maki.

Bridges Discord messages to the brain loop via NATS pub/sub.
Listens in #maki-general and DMs, relays responses from stem.
"""

import asyncio
import json
import logging
import os
import signal
import uuid

import discord
from maki_common import (
    PendingQueues,
    configure_logging,
    connect_nats,
    init_kv,
    kv_acquire_lease,
    spawn_background,
    subscribe_supervised,
)
from maki_common.settings import NATS_TOKEN, NATS_URL
from maki_common.subjects import (
    EARS_IMMUNE_OUT,
    EARS_IN,
    EARS_OUT,
    EARS_SEARCH,
    EARS_VITALS_OUT,
    IMMUNE_ALERT,
    IMMUNE_COMMAND,
)

from maki_ears.dedup import claim_or_skip
from maki_ears.trading import (
    TradeProposalView,
    handle_trade_command,
    handle_trade_interaction,
)

configure_logging()
log = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GENERAL_CHANNEL_NAME = os.environ.get("GENERAL_CHANNEL_NAME", "maki-general")
OWNER_ID = int(os.environ.get("OWNER_ID", "690270213370806313"))
VITALS_CHANNEL_NAME = os.environ.get("VITALS_CHANNEL_NAME", "maki-alerts")
IMMUNE_CHANNEL_NAME = os.environ.get("IMMUNE_CHANNEL_NAME", "maki-immune")
TRADING_CHANNEL_NAME = os.environ.get("TRADING_CHANNEL_NAME", "maki-trading")

# Timeout (seconds) after receiving the last chunk before assuming done.
# Safety net in case the done signal is lost in transit.
# Set high (10 min) because tool-heavy turns can go minutes without text output.
CHUNK_INACTIVITY_TIMEOUT = 600.0

_nc = None
_js = None
_dedup_kv = None
_lock_kv = None
_pending = PendingQueues()
_immune_pending = PendingQueues()
DEDUP_BUCKET = "maki-ears-dedup"
LOCK_BUCKET = "maki-lock"
LEADER_KEY = "ears.leader"
LEADER_TTL = 15  # seconds — must renew before this expires
INSTANCE_ID = f"ears-{uuid.uuid4().hex[:8]}"
_general_channel_ids: set[int] = set()
_vitals_channel_ids: set[int] = set()
_immune_channel_ids: set[int] = set()
_trading_channel_ids: set[int] = set()

_bot = None
_shutdown_event: asyncio.Event | None = None


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    """Install SIGTERM/SIGINT handlers that set the shutdown event.

    Without this, SIGTERM from Kubernetes goes straight to a forced kill:
    the NATS connection drops mid-publish, in-flight Discord sends die, and
    the dedup KV never sees a clean disconnect. Setting an asyncio.Event lets
    the main loop break, close the bot, and close NATS cleanly.
    """

    def _handle(signame: str) -> None:
        log.info("Shutdown signal received", extra={"signal": signame})
        event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle, sig.name)
        except NotImplementedError:
            # Windows / restricted envs — ctrl-c still raises KeyboardInterrupt.
            pass


def _discover_channel(guild, channel_name: str, channel_ids: set[int], label: str):
    """Find a channel by name in a guild and register its ID."""
    for channel in guild.text_channels:
        if channel.name == channel_name:
            channel_ids.add(channel.id)
            log.info(
                f"{label} channel found",
                extra={
                    "channel": channel.name,
                    "guild": guild.name,
                    "channel_id": channel.id,
                },
            )


class MakiDiscordClient(discord.Client):
    """Discord client for Maki — recreated on each leadership acquisition."""

    async def on_ready(self):
        log.info("Discord connected", extra={"bot_name": self.user.name, "bot_id": self.user.id})

        for guild in self.guilds:
            _discover_channel(guild, GENERAL_CHANNEL_NAME, _general_channel_ids, "General")
            _discover_channel(guild, VITALS_CHANNEL_NAME, _vitals_channel_ids, "Vitals")
            _discover_channel(guild, IMMUNE_CHANNEL_NAME, _immune_channel_ids, "Immune")
            _discover_channel(guild, TRADING_CHANNEL_NAME, _trading_channel_ids, "Trading")

        if not _general_channel_ids:
            log.warning("No general channel found", extra={"channel_name": GENERAL_CHANNEL_NAME})
        if not _immune_channel_ids:
            log.warning("No immune channel found", extra={"channel_name": IMMUNE_CHANNEL_NAME})
        if not _trading_channel_ids:
            log.info(
                "No trading channel — output goes to general",
                extra={"channel_name": TRADING_CHANNEL_NAME},
            )

    async def search_channel(
        self,
        query: str,
        channel_id: int | None = None,
        limit: int = 25,
    ) -> dict:
        """Search guild message history via Discord's indexed search API.

        Discord returns 202 with ``retry_after`` when the index isn't ready yet.
        We detect this by the absence of the ``messages`` key and retry up to 5
        times, honouring the back-off value Discord provides.

        Returns a dict with shape::

            {"ok": True, "messages": [...], "total_results": int}
            {"ok": False, "error": str}
        """
        if not self.guilds:
            return {"ok": False, "error": "Bot not in any guild"}

        guild_id = self.guilds[0].id
        limit = max(1, min(limit, 25))

        params: dict = {"content": query, "limit": limit, "sort_by": "relevance", "sort_order": "desc"}
        if channel_id:
            params["channel_id"] = str(channel_id)

        from discord.http import Route

        route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild_id)

        data: dict = {}
        _channel_fallback_done = False
        for attempt in range(5):
            try:
                data = await self.http.request(route, params=params)
            except discord.HTTPException as exc:
                # 403 with a channel scope → Discord doesn't allow per-channel search
                # scoping via this bot token. Fall back to guild-wide once, silently.
                if exc.status == 403 and "channel_id" in params and not _channel_fallback_done:
                    log.warning(
                        "Channel-scoped search returned 403, falling back to guild-wide",
                        extra={"channel_id": params.pop("channel_id")},
                    )
                    _channel_fallback_done = True
                    continue
                return {"ok": False, "error": f"Discord API error {exc.status}: {exc.text}"}

            if "messages" in data:
                break  # 200 — results ready

            # 202 — index not ready, Discord says how long to wait
            retry_after = float(data.get("retry_after", 5.0))
            log.info(
                "Discord search index not ready, retrying",
                extra={"attempt": attempt + 1, "retry_after": retry_after},
            )
            await asyncio.sleep(retry_after)
        else:
            return {"ok": False, "error": "Discord search index not ready after retries"}

        # Each element of data["messages"] is a list: first item is the matched
        # message, remaining items are context. We only need the matched message.
        hits = []
        for group in data.get("messages", []):
            if not group:
                continue
            m = group[0]
            hits.append(
                {
                    "id": m.get("id"),
                    "content": m.get("content", ""),
                    "author": (m.get("author") or {}).get("username", "unknown"),
                    "timestamp": m.get("timestamp"),
                    "channel_id": m.get("channel_id"),
                }
            )

        return {"ok": True, "messages": hits, "total_results": data.get("total_results", len(hits))}

    async def on_interaction(self, interaction: discord.Interaction):
        """Route Discord component interactions to their feature handlers."""
        if await handle_trade_interaction(interaction, _nc):
            return

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return

        if message.author.id != OWNER_ID:
            await message.channel.send("Get your own, perv!")
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_general = message.channel.id in _general_channel_ids
        is_immune = message.channel.id in _immune_channel_ids

        if not is_dm and not is_general and not is_immune:
            return

        content = message.content.strip()
        if not content:
            return

        log.info(
            "Message received",
            extra={
                "author": message.author.name,
                "channel_id": message.channel.id,
                "content_len": len(content),
                "is_immune": is_immune,
            },
        )

        # Route immune channel messages directly to immune, bypassing cortex
        if is_immune:
            await _handle_immune_command(message, content)
            return

        # !trade command — manual trade log, bypass cortex
        if content.lower().startswith("!trade"):
            await handle_trade_command(message, content, _nc, _dedup_kv)
            return

        # !loop command — trigger loop, immediate ack, no typing indicator
        if content.lower().startswith("!loop"):
            await _handle_loop_command(message, content)
            return

        # Dedup: if another ears instance already published this message, skip.
        # On transient KV errors, fail-open (process anyway) rather than silently
        # drop the user's message — see #416. Dedup is defence-in-depth for
        # blue/green; with a single replica today, a false-negative dupe is far
        # worse than a rare double-process.
        try:
            if not await claim_or_skip(_dedup_kv, str(message.id), "message"):
                return
        except Exception:
            log.warning(
                "Dedup KV failed — fail-open, processing message anyway",
                extra={"message_id": str(message.id)},
                exc_info=True,
            )

        payload = {
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "username": message.author.name,
            "content": content,
        }

        try:
            await _nc.publish(EARS_IN, json.dumps(payload).encode())
            log.info("Published to NATS", extra={"subject": EARS_IN})
        except Exception:
            log.exception("Failed to publish message to NATS")
            await message.channel.send("Sorry, I couldn't process that right now.")
            return

        thinking_emoji = "\U0001f363"  # 🍣
        await message.add_reaction(thinking_emoji)

        received_any = False
        try:
            async with _pending.session(str(message.id)) as queue, message.channel.typing():
                while True:
                    # Use shorter timeout once we've received at least one chunk.
                    # If done signal is lost, we don't hang forever.
                    timeout = CHUNK_INACTIVITY_TIMEOUT if received_any else 1860.0

                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=timeout)
                    except TimeoutError:
                        if received_any:
                            log.warning(
                                "No done signal received after last chunk, assuming done",
                                extra={"message_id": str(message.id)},
                            )
                        else:
                            await message.channel.send("Sorry, I took too long thinking about that. Try again?")
                        break

                    # Skip reaction messages from cortex
                    if "reaction" in data:
                        continue

                    chunk = data.get("response", "")
                    done = data.get("done", False)

                    if chunk:
                        await _send_response(message.channel, chunk)
                        received_any = True

                    if done:
                        break
        finally:
            try:
                await message.remove_reaction(thinking_emoji, self.user)
            except Exception:
                pass


async def _handle_immune_command(message: discord.Message, content: str):
    """Handle messages in #maki-immune — forward to immune as direct commands."""
    # Dedup: if another ears instance already published this command, skip.
    # Fail-open on transient KV errors so a NATS blip doesn't silently swallow
    # an immune command (which is precisely when Adi is most likely to be
    # investigating something that just broke). See #416.
    try:
        if not await claim_or_skip(_dedup_kv, str(message.id), "immune command"):
            return
    except Exception:
        log.warning(
            "Dedup KV failed — fail-open, processing immune command anyway",
            extra={"message_id": str(message.id)},
            exc_info=True,
        )

    payload = {
        "message_id": str(message.id),
        "command": content,
        "username": message.author.name,
        "timestamp": asyncio.get_event_loop().time(),
    }

    try:
        await _nc.publish(IMMUNE_COMMAND, json.dumps(payload).encode())
        log.info("Immune command published", extra={"subject": IMMUNE_COMMAND})
    except Exception:
        log.exception("Failed to publish immune command")
        await message.channel.send("Failed to reach immune system.")
        return

    thinking_emoji = "\U0001f6e1\ufe0f"  # 🛡️
    await message.add_reaction(thinking_emoji)

    # Wait for immune's response
    try:
        async with _immune_pending.session(str(message.id)) as queue, message.channel.typing():
            try:
                # Immune gets 5 minutes — it may need to investigate with Claude
                data = await asyncio.wait_for(queue.get(), timeout=300.0)
                response = data.get("response", "")
                if response:
                    await _send_response(message.channel, response)
                else:
                    await message.channel.send("Immune processed command but had nothing to report.")
            except TimeoutError:
                await message.channel.send("Immune didn't respond in time. It may still be working on it.")
    finally:
        try:
            await message.remove_reaction(thinking_emoji, _bot.user)
        except Exception:
            pass


async def _handle_loop_command(message: discord.Message, content: str) -> None:
    """Handle ``!loop <name>`` — forward to stem via EARS_IN, immediate ack, no typing."""
    # Fail-open on transient KV errors — see #416.
    try:
        if not await claim_or_skip(_dedup_kv, str(message.id), "loop command"):
            return
    except Exception:
        log.warning(
            "Dedup KV failed — fail-open, processing loop command anyway",
            extra={"message_id": str(message.id)},
            exc_info=True,
        )

    tokens = content.strip().split()
    if len(tokens) < 2:
        await message.channel.send("Usage: `!loop <name>`")
        return

    loop_name = tokens[1]

    payload = {
        "message_id": str(message.id),
        "channel_id": str(message.channel.id),
        "username": message.author.name,
        "content": content,
    }

    try:
        await _nc.publish(EARS_IN, json.dumps(payload).encode())
        log.info("Loop command published", extra={"subject": EARS_IN, "loop": loop_name})
    except Exception:
        log.exception("Failed to publish loop command")
        await message.channel.send("Failed to trigger loop — NATS unavailable.")
        return

    await message.channel.send(f"⏳ Triggering **{loop_name}** loop...")


async def _handle_search_request(msg) -> None:
    """Process one Discord history search request and reply via NATS."""
    try:
        data = json.loads(msg.data.decode())
        query = data.get("query", "").strip()
        channel_id = data.get("channel_id")
        limit = int(data.get("limit", 25))

        if not query:
            result: dict = {"ok": False, "error": "query is required"}
        elif _bot is None or _bot.is_closed():
            result = {"ok": False, "error": "Discord not connected on this instance"}
        else:
            result = await _bot.search_channel(query=query, channel_id=channel_id, limit=limit)

        if msg.reply:
            await _nc.publish(msg.reply, json.dumps(result).encode())

        log.info(
            "Search request handled",
            extra={"query": query[:60], "ok": result.get("ok"), "hits": len(result.get("messages", []))},
        )
    except Exception:
        log.exception("Error handling search request")
        if msg.reply:
            await _nc.publish(msg.reply, json.dumps({"ok": False, "error": "Internal search error"}).encode())


async def _dispatch_search(msg) -> None:
    """Spawn-and-return: per-message task so a slow Discord call never blocks the supervisor."""
    spawn_background(_handle_search_request(msg), name="ears.search_request")


async def _search_listener() -> None:
    """Subscribe to maki.ears.search — active only while this instance is leader.

    Started as a task when leadership is acquired and cancelled when the
    Discord bot disconnects or leadership is lost. Wrapped in
    ``subscribe_supervised`` so a NATS reconnect / stream drain re-subscribes
    instead of silently terminating the listener (issue #175).
    """
    try:
        await subscribe_supervised(
            _nc,
            EARS_SEARCH,
            _dispatch_search,
            name="ears.search",
        )
    finally:
        log.info("Search listener stopped")


async def _handle_ears_out(msg) -> None:
    """Process one EARS_OUT message — chat reply, trade proposal, or loop output."""
    try:
        data = json.loads(msg.data.decode())

        # Chat response (request/reply via pending queue)
        message_id = data.get("message_id")
        if message_id:
            if _pending.push(message_id, data):
                log.info(
                    "Response chunk pushed",
                    extra={"message_id": message_id, "done": data.get("done", False)},
                )
            else:
                log.warning("Response for unknown message", extra={"message_id": message_id})
            return

        # Trade proposal with Buy/Sell + Skip buttons
        proposal_id = data.get("proposal_id")
        text = data.get("text", "")
        if proposal_id and data.get("components") and text:
            if _bot and not _bot.is_closed():
                direction = data.get("direction", "buy")
                symbol = data.get("symbol", "")
                entry_price = float(data.get("entry_price") or 0.0)
                view = TradeProposalView(proposal_id, symbol, direction, entry_price)
                channel_kind = "trading" if _trading_channel_ids else "general"
                target_ids = _trading_channel_ids or _general_channel_ids
                if not target_ids:
                    log.warning(
                        "No trading/general channel available, trade proposal dropped",
                        extra={"proposal_id": proposal_id, "channel": channel_kind},
                    )
                for channel_id in target_ids:
                    channel = _bot.get_channel(channel_id)
                    if channel:
                        await channel.send(text, view=view)
                        log.info(
                            "Trade proposal posted",
                            extra={"proposal_id": proposal_id, "channel_id": channel_id},
                        )
            else:
                log.warning("Trade proposal dropped — Discord not connected", extra={"proposal_id": proposal_id})
            return

        # Loop output (fire-and-forget)
        if not text:
            return

        turn_id = data.get("turn_id", "unknown")
        log.info("Loop output received", extra={"turn_id": turn_id, "text_len": len(text)})

        # Routing intent comes from the publisher via the ``channel`` field.
        # Default to "general" for back-compat with payloads that don't set it.
        channel_kind = data.get("channel", "general")
        channel_map = {
            "general": _general_channel_ids,
            "trading": _trading_channel_ids or _general_channel_ids,
            "vitals": _vitals_channel_ids or _general_channel_ids,
        }
        target_ids = channel_map.get(channel_kind)
        if target_ids is None:
            log.warning(
                "Unknown channel kind — falling back to general",
                extra={"channel": channel_kind, "turn_id": turn_id},
            )
            target_ids = _general_channel_ids
        if not target_ids:
            log.warning(
                "No channel available, loop output dropped",
                extra={"channel": channel_kind, "turn_id": turn_id},
            )
        for channel_id in target_ids:
            channel = _bot.get_channel(channel_id)
            if channel:
                await _send_response(channel, text)
                log.info(
                    "Loop output posted",
                    extra={"channel_id": channel_id, "channel": channel_kind},
                )

    except Exception:
        log.exception("Error processing EARS_OUT message")


async def _out_listener():
    """Unified listener for all EARS_OUT messages.

    Payload with ``message_id`` → push to pending queue (chat request/reply).
    Payload with ``text`` → fire-and-forget post to #maki-general (loop output).

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently terminating Discord output (issue #175).
    """
    await subscribe_supervised(
        _nc,
        EARS_OUT,
        _handle_ears_out,
        name="ears.out",
    )


async def _handle_immune_response(msg) -> None:
    """Process one EARS_IMMUNE_OUT message and push to pending queue."""
    try:
        data = json.loads(msg.data.decode())
        message_id = data.get("message_id", "")

        if _immune_pending.push(message_id, data):
            log.info("Immune response pushed", extra={"message_id": message_id})
        else:
            log.warning("Immune response for unknown message", extra={"message_id": message_id})
    except Exception:
        log.exception("Error processing immune response")


async def _immune_response_listener():
    """Subscribe to NATS for immune command responses.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently terminating (issue #175).
    """
    await subscribe_supervised(
        _nc,
        EARS_IMMUNE_OUT,
        _handle_immune_response,
        name="ears.immune_out",
    )


async def _handle_vitals(msg) -> None:
    """Process one vitals digest and post to #maki-alerts.

    ``subscribe_supervised`` handles the ack on our behalf (auto_ack defaults
    to True for JetStream subs) — ACK on success, NAK on uncaught handler
    exception so JS redelivers per the consumer's max_deliver (issue #221).
    This handler currently swallows Discord errors internally; rework the
    broad ``try/except`` below if at-least-once posting becomes load-bearing.
    """
    try:
        data = json.loads(msg.data.decode())
        digest = data.get("digest", "")
        if not digest:
            return

        log.info("Health digest received", extra={"digest_len": len(digest)})
        for channel_id in _vitals_channel_ids:
            channel = _bot.get_channel(channel_id)
            if channel:
                await _send_response(channel, digest)
                log.info("Digest posted", extra={"channel": VITALS_CHANNEL_NAME, "channel_id": channel_id})

        if not _vitals_channel_ids:
            log.warning("No vitals channel available, digest dropped")
    except Exception:
        log.exception("Error processing vitals")


async def _vitals_listener():
    """Consume health digests from JetStream and post to #maki-general.

    Wrapped in ``subscribe_supervised`` so a JS reconnect / stream drain
    re-subscribes the durable consumer instead of silently terminating
    (issue #175). auto_ack=True (JS default) ACKs on success and NAKs
    uncaught handler exceptions for redelivery (issue #221).
    """
    await subscribe_supervised(
        _nc,
        EARS_VITALS_OUT,
        _handle_vitals,
        js=_js,
        durable=f"ears-vitals-{INSTANCE_ID}",
        deliver_policy="new",
        name="ears.vitals",
    )


async def _handle_alert(msg) -> None:
    """Process one immune alert and post to #maki-alerts."""
    try:
        data = json.loads(msg.data.decode())
        alert = data.get("alert", "")
        if not alert:
            return

        alert_text = f"**ALERT** {alert}"
        log.info("Alert received", extra={"alert_preview": alert[:100]})
        for channel_id in _vitals_channel_ids:
            channel = _bot.get_channel(channel_id)
            if channel:
                await _send_response(channel, alert_text)
                log.info("Alert posted", extra={"channel": VITALS_CHANNEL_NAME, "channel_id": channel_id})

        if not _vitals_channel_ids:
            log.warning("No vitals channel available, alert dropped")
    except Exception:
        log.exception("Error processing alert")


async def _alert_listener():
    """Consume immune alerts from JetStream and post to #maki-general.

    Wrapped in ``subscribe_supervised`` so a JS reconnect / stream drain
    re-subscribes the durable consumer instead of silently terminating
    (issue #175). auto_ack=True (JS default) ACKs on success and NAKs
    uncaught handler exceptions for redelivery (issue #221).
    """
    await subscribe_supervised(
        _nc,
        IMMUNE_ALERT,
        _handle_alert,
        js=_js,
        durable=f"ears-alert-{INSTANCE_ID}",
        deliver_policy="new",
        name="ears.alert",
    )


async def _send_response(channel, text: str):
    """Send a response to Discord, splitting if necessary."""
    while text:
        chunk = text[:2000]
        if len(text) > 2000:
            last_newline = chunk.rfind("\n")
            if last_newline > 1000:
                chunk = text[:last_newline]
        await channel.send(chunk)
        text = text[len(chunk) :]


async def _try_acquire_leadership() -> bool:
    """Try to become the ears leader via the shared KV-lease primitive."""
    return await kv_acquire_lease(_lock_kv, LEADER_KEY, LEADER_TTL, INSTANCE_ID, allow_renew=True)


def _create_bot():
    """Create a fresh Discord client instance."""
    global _bot
    intents = discord.Intents.default()
    intents.message_content = True
    _bot = MakiDiscordClient(intents=intents)
    return _bot


async def _leader_renewal_loop():
    """Renew leadership claim every LEADER_TTL/2 seconds while Discord is connected."""
    interval = LEADER_TTL / 2
    while True:
        await asyncio.sleep(interval)
        try:
            if not await _try_acquire_leadership():
                log.warning("Lost leadership, shutting down Discord bot")
                await _bot.close()
                return
        except Exception:
            log.exception("Leader renewal error")


async def main():
    global _nc, _js, _dedup_kv, _lock_kv, _bot, _shutdown_event

    log.info("maki-ears starting", extra={"nats_url": NATS_URL, "instance_id": INSTANCE_ID})

    _shutdown_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), _shutdown_event)

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    _lock_kv = await init_kv(_js, LOCK_BUCKET)

    # Dedup bucket with 5-minute TTL — prevents duplicate Discord event processing
    try:
        _dedup_kv = await _js.key_value(DEDUP_BUCKET)
    except Exception:
        _dedup_kv = await _js.create_key_value(bucket=DEDUP_BUCKET, ttl=300)

    # NATS listeners run always — harmless when not leader
    # (response listeners silently drop unmatched messages,
    #  outbound listeners have no channels to post to without Discord).
    # ``spawn_background`` anchors these against GC and logs any uncaught
    # exception — a bare ``create_task`` would let a listener silently vanish
    # if the caller's weak-ref lapses (issue #123).
    spawn_background(_out_listener(), name="ears.out_listener")
    spawn_background(_immune_response_listener(), name="ears.immune_response_listener")
    spawn_background(_vitals_listener(), name="ears.vitals_listener")
    spawn_background(_alert_listener(), name="ears.alert_listener")

    try:
        # Leader election loop — only the leader connects to Discord.
        # Breaks on _shutdown_event so SIGTERM triggers the cleanup below.
        while not _shutdown_event.is_set():
            if await _try_acquire_leadership():
                log.info("Acquired leadership — connecting to Discord", extra={"instance_id": INSTANCE_ID})

                # Fresh client each time — discord.py closes the aiohttp session on
                # bot.close(), making the old instance unusable.
                _bot = _create_bot()
                _general_channel_ids.clear()
                _vitals_channel_ids.clear()
                _immune_channel_ids.clear()
                _trading_channel_ids.clear()

                # ``spawn_background`` for renewal_task gives us exception logging;
                # the returned Task is still assigned for the ``.cancel()`` call in
                # the ``finally`` block below (issue #123). search_task keeps a bare
                # create_task because its handle is both retained here and awaited
                # via cancel — no risk of GC or silent exception loss.
                renewal_task = spawn_background(_leader_renewal_loop(), name="ears.leader_renewal")
                search_task = asyncio.create_task(_search_listener())

                # Race the bot's lifetime against the shutdown signal so SIGTERM
                # can unblock the otherwise indefinite _bot.start() call.
                bot_task = asyncio.create_task(_bot.start(DISCORD_TOKEN))
                shutdown_wait = asyncio.create_task(_shutdown_event.wait())

                try:
                    done, _ = await asyncio.wait(
                        {bot_task, shutdown_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if bot_task in done:
                        exc = bot_task.exception()
                        if exc is not None:
                            log.error("Discord bot disconnected", exc_info=exc)
                    else:
                        # Shutdown fired first — close the bot to unblock start().
                        log.info("Shutdown requested, closing Discord bot")
                        try:
                            await _bot.close()
                        except Exception:
                            log.exception("Error closing Discord bot")
                        try:
                            await bot_task
                        except Exception:
                            log.exception("Discord bot shutdown error")
                finally:
                    if not shutdown_wait.done():
                        shutdown_wait.cancel()
                    # Cancel both tasks tied to this bot session. Leaving the
                    # renewal loop alive across reconnects lets an orphaned task
                    # close the *next* bot when its CAS happens to fail — the
                    # global _bot rebinding makes the leak silently destructive.
                    # See #177.
                    renewal_task.cancel()
                    search_task.cancel()
                    log.info("Discord bot stopped, returning to standby")
            else:
                log.info("Another instance is leader, standing by", extra={"instance_id": INSTANCE_ID})

            # Interruptible sleep — shutdown breaks out immediately.
            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=LEADER_TTL)
            except TimeoutError:
                pass
    finally:
        log.info("maki-ears shutting down")
        if _bot is not None:
            try:
                if not _bot.is_closed():
                    await _bot.close()
            except Exception:
                log.exception("Error closing Discord bot during shutdown")
        if _nc is not None:
            try:
                await _nc.close()
                log.info("NATS connection closed")
            except Exception:
                log.exception("Error closing NATS connection")


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
