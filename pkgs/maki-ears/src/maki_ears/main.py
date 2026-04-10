"""maki-ears: Discord interface for Maki.

Bridges Discord messages to the brain loop via NATS pub/sub.
Listens in #maki-general and DMs, relays responses from stem.
"""

import asyncio
import json
import logging
import os
import uuid

import discord
from maki_common import PendingQueues, configure_logging, connect_nats, init_kv
from maki_common.subjects import (
    EARS_IMMUNE_OUT,
    EARS_IN,
    EARS_INTERACTION,
    EARS_OUT,
    EARS_SEARCH,
    EARS_VITALS_OUT,
    IMMUNE_ALERT,
    IMMUNE_COMMAND,
)

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GENERAL_CHANNEL_NAME = os.environ.get("GENERAL_CHANNEL_NAME", "maki-general")
OWNER_ID = int(os.environ.get("OWNER_ID", "690270213370806313"))
VITALS_CHANNEL_NAME = os.environ.get("VITALS_CHANNEL_NAME", "maki-general")
IMMUNE_CHANNEL_NAME = os.environ.get("IMMUNE_CHANNEL_NAME", "maki-immune")

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

_bot = None


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

        if not _general_channel_ids:
            log.warning("No general channel found", extra={"channel_name": GENERAL_CHANNEL_NAME})
        if not _immune_channel_ids:
            log.warning("No immune channel found", extra={"channel_name": IMMUNE_CHANNEL_NAME})

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

        # Dedup: if another ears instance already published this message, skip
        msg_key = f"msg.{message.id}"
        try:
            await _dedup_kv.create(msg_key, b"1")
        except Exception:
            log.info("Dedup: message already claimed by another instance", extra={"message_id": str(message.id)})
            return

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
        queue = _pending.create(str(message.id))
        try:
            async with message.channel.typing():
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
            _pending.remove(str(message.id))
            try:
                await message.remove_reaction(thinking_emoji, self.user)
            except Exception:
                pass


class _TradeButton(discord.ui.Button):
    """A single Accept or Skip button for a trade proposal."""

    def __init__(self, label: str, custom_id: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, custom_id=custom_id, style=style)

    async def callback(self, interaction: discord.Interaction) -> None:
        # custom_id format: "trade:{proposal_id}:{action}"
        parts = self.custom_id.split(":")
        proposal_id, action = parts[1], parts[2]

        await interaction.response.defer()

        outcome_label = "✅ Accepted" if action == "accept" else "⏭️ Skipped"
        try:
            await interaction.message.edit(
                content=interaction.message.content + f"\n\n_{outcome_label} by {interaction.user.name}_",
                view=None,
            )
        except Exception:
            log.warning("Failed to edit proposal message after interaction", exc_info=True)

        if _nc:
            subject = f"{EARS_INTERACTION}.{proposal_id}"
            payload = {"proposal_id": proposal_id, "action": action, "user": str(interaction.user.name)}
            await _nc.publish(subject, json.dumps(payload).encode())
            log.info("Trade interaction published", extra={"proposal_id": proposal_id, "action": action})


class _TradeProposalView(discord.ui.View):
    """Discord button view attached to an outgoing trade proposal message."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(timeout=300)  # buttons expire after 5 minutes
        self.add_item(_TradeButton("✅ Accept", f"trade:{proposal_id}:accept", discord.ButtonStyle.success))
        self.add_item(_TradeButton("⏭️ Skip", f"trade:{proposal_id}:skip", discord.ButtonStyle.secondary))


async def _handle_immune_command(message: discord.Message, content: str):
    """Handle messages in #maki-immune — forward to immune as direct commands."""
    # Dedup: if another ears instance already published this command, skip
    msg_key = f"msg.{message.id}"
    try:
        await _dedup_kv.create(msg_key, b"1")
    except Exception:
        log.info("Dedup: immune command already claimed", extra={"message_id": str(message.id)})
        return

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
    queue = _immune_pending.create(str(message.id))
    try:
        async with message.channel.typing():
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
        _immune_pending.remove(str(message.id))
        try:
            await message.remove_reaction(thinking_emoji, _bot.user)
        except Exception:
            pass


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


async def _search_listener() -> None:
    """Subscribe to maki.ears.search — active only while this instance is leader.

    Started as a task when leadership is acquired and cancelled (with automatic
    unsubscription) when the Discord bot disconnects or leadership is lost.
    Each incoming message is handled in its own task so a slow Discord API call
    never blocks the subscription loop.
    """
    sub = await _nc.subscribe(EARS_SEARCH)
    log.info("Subscribed to search requests", extra={"subject": EARS_SEARCH})
    try:
        async for msg in sub.messages:
            asyncio.create_task(_handle_search_request(msg))
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await sub.unsubscribe()
        except Exception:
            pass
        log.info("Search listener stopped")


async def _out_listener():
    """Unified listener for all EARS_OUT messages.

    Payload with ``message_id`` → push to pending queue (chat request/reply).
    Payload with ``text`` → fire-and-forget post to #maki-general (loop output).
    """
    sub = await _nc.subscribe(EARS_OUT)
    log.info("Subscribed", extra={"subject": EARS_OUT})
    async for msg in sub.messages:
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
                continue

            # Trade proposal with Accept / Skip buttons
            proposal_id = data.get("proposal_id")
            text = data.get("text", "")
            if proposal_id and data.get("components") and text:
                if _bot and not _bot.is_closed():
                    view = _TradeProposalView(proposal_id)
                    for channel_id in _general_channel_ids:
                        channel = _bot.get_channel(channel_id)
                        if channel:
                            await channel.send(text, view=view)
                            log.info(
                                "Trade proposal posted",
                                extra={"proposal_id": proposal_id, "channel_id": channel_id},
                            )
                else:
                    log.warning("Trade proposal dropped — Discord not connected", extra={"proposal_id": proposal_id})
                continue

            # Loop output (fire-and-forget → general channel)
            if not text:
                continue

            turn_id = data.get("turn_id", "unknown")
            log.info("Loop output received", extra={"turn_id": turn_id, "text_len": len(text)})

            for channel_id in _general_channel_ids:
                channel = _bot.get_channel(channel_id)
                if channel:
                    await _send_response(channel, text)
                    log.info("Loop output posted", extra={"channel_id": channel_id})

        except Exception:
            log.exception("Error processing EARS_OUT message")


async def _immune_response_listener():
    """Subscribe to NATS for immune command responses."""
    sub = await _nc.subscribe(EARS_IMMUNE_OUT)
    log.info("Subscribed", extra={"subject": EARS_IMMUNE_OUT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            message_id = data.get("message_id", "")

            if _immune_pending.push(message_id, data):
                log.info("Immune response pushed", extra={"message_id": message_id})
            else:
                log.warning("Immune response for unknown message", extra={"message_id": message_id})
        except Exception:
            log.exception("Error processing immune response")


async def _vitals_listener():
    """Consume health digests from JetStream and post to #maki-general."""
    sub = await _js.subscribe(EARS_VITALS_OUT, durable=f"ears-vitals-{INSTANCE_ID}", deliver_policy="new")
    log.info("JetStream subscribed", extra={"subject": EARS_VITALS_OUT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            digest = data.get("digest", "")
            if not digest:
                await msg.ack()
                continue

            log.info("Health digest received", extra={"digest_len": len(digest)})
            for channel_id in _vitals_channel_ids:
                channel = _bot.get_channel(channel_id)
                if channel:
                    await _send_response(channel, digest)
                    log.info("Digest posted", extra={"channel": VITALS_CHANNEL_NAME, "channel_id": channel_id})

            if not _vitals_channel_ids:
                log.warning("No vitals channel available, digest dropped")
            await msg.ack()
        except Exception:
            log.exception("Error processing vitals")


async def _alert_listener():
    """Consume immune alerts from JetStream and post to #maki-general."""
    sub = await _js.subscribe(IMMUNE_ALERT, durable=f"ears-alert-{INSTANCE_ID}", deliver_policy="new")
    log.info("JetStream subscribed", extra={"subject": IMMUNE_ALERT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            alert = data.get("alert", "")
            if not alert:
                await msg.ack()
                continue

            alert_text = f"**ALERT** {alert}"
            log.info("Alert received", extra={"alert_preview": alert[:100]})
            for channel_id in _vitals_channel_ids:
                channel = _bot.get_channel(channel_id)
                if channel:
                    await _send_response(channel, alert_text)
                    log.info("Alert posted", extra={"channel": VITALS_CHANNEL_NAME, "channel_id": channel_id})

            if not _vitals_channel_ids:
                log.warning("No vitals channel available, alert dropped")
            await msg.ack()
        except Exception:
            log.exception("Error processing alert")


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
    """Try to become the ears leader via NATS KV CAS."""
    import json as _json
    import time as _time

    now = _time.time()
    claim = _json.dumps({"instance": INSTANCE_ID, "claimed_at": now}).encode()

    try:
        entry = await _lock_kv.get(LEADER_KEY)
        data = _json.loads(entry.value.decode())
        # If current leader's claim is fresh, we're not the leader
        if now - data.get("claimed_at", 0) < LEADER_TTL:
            if data.get("instance") == INSTANCE_ID:
                # We're already the leader — renew
                await _lock_kv.update(LEADER_KEY, claim, entry.revision)
                return True
            return False
        # Claim expired — try to take over
        await _lock_kv.update(LEADER_KEY, claim, entry.revision)
        return True
    except Exception:
        try:
            await _lock_kv.create(LEADER_KEY, claim)
            return True
        except Exception:
            return False


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
    global _nc, _js, _dedup_kv, _lock_kv, _bot

    log.info("maki-ears starting", extra={"nats_url": NATS_URL, "instance_id": INSTANCE_ID})

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
    #  outbound listeners have no channels to post to without Discord)
    asyncio.create_task(_out_listener())
    asyncio.create_task(_immune_response_listener())
    asyncio.create_task(_vitals_listener())
    asyncio.create_task(_alert_listener())

    # Leader election loop — only the leader connects to Discord
    while True:
        if await _try_acquire_leadership():
            log.info("Acquired leadership — connecting to Discord", extra={"instance_id": INSTANCE_ID})

            # Fresh client each time — discord.py closes the aiohttp session on
            # bot.close(), making the old instance unusable.
            _bot = _create_bot()
            _general_channel_ids.clear()
            _vitals_channel_ids.clear()
            _immune_channel_ids.clear()

            asyncio.create_task(_leader_renewal_loop())
            search_task = asyncio.create_task(_search_listener())

            try:
                await _bot.start(DISCORD_TOKEN)
            except Exception:
                log.exception("Discord bot disconnected")
            finally:
                search_task.cancel()
                log.info("Discord bot stopped, returning to standby")
        else:
            log.info("Another instance is leader, standing by", extra={"instance_id": INSTANCE_ID})

        await asyncio.sleep(LEADER_TTL)

    await _nc.close()
    log.info("NATS connection closed")


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
