"""maki-ears: Discord interface for Maki.

Bridges Discord messages to the brain loop via NATS pub/sub.
Listens in #maki-general and DMs, relays responses from stem.
"""

import asyncio
import json
import logging
import os

import discord
from maki_common import PendingQueues, configure_logging, connect_nats
from maki_common.subjects import (
    EARS_IMMUNE_OUT,
    EARS_MESSAGE_IN,
    EARS_MESSAGE_OUT,
    EARS_REMINDER_OUT,
    EARS_THOUGHT_OUT,
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
THOUGHTS_CHANNEL_NAME = os.environ.get("THOUGHTS_CHANNEL_NAME", "maki-thoughts")
VITALS_CHANNEL_NAME = os.environ.get("VITALS_CHANNEL_NAME", "maki-vitals")
REMINDERS_CHANNEL_NAME = os.environ.get("REMINDERS_CHANNEL_NAME", "maki-reminders")
IMMUNE_CHANNEL_NAME = os.environ.get("IMMUNE_CHANNEL_NAME", "maki-immune")

# Timeout (seconds) after receiving the last chunk before assuming done.
# Safety net in case the done signal is lost in transit.
# Set high (10 min) because tool-heavy turns can go minutes without text output.
CHUNK_INACTIVITY_TIMEOUT = 600.0

_nc = None
_pending = PendingQueues()
_immune_pending = PendingQueues()
_general_channel_ids: set[int] = set()
_thoughts_channel_ids: set[int] = set()
_vitals_channel_ids: set[int] = set()
_reminders_channel_ids: set[int] = set()
_immune_channel_ids: set[int] = set()

intents = discord.Intents.default()
intents.message_content = True
_bot = discord.Client(intents=intents)


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


@_bot.event
async def on_ready():
    log.info("Discord connected", extra={"bot_name": _bot.user.name, "bot_id": _bot.user.id})

    for guild in _bot.guilds:
        _discover_channel(guild, GENERAL_CHANNEL_NAME, _general_channel_ids, "General")
        _discover_channel(guild, THOUGHTS_CHANNEL_NAME, _thoughts_channel_ids, "Thoughts")
        _discover_channel(guild, VITALS_CHANNEL_NAME, _vitals_channel_ids, "Vitals")
        _discover_channel(guild, REMINDERS_CHANNEL_NAME, _reminders_channel_ids, "Reminders")
        _discover_channel(guild, IMMUNE_CHANNEL_NAME, _immune_channel_ids, "Immune")

    if not _general_channel_ids:
        log.warning("No general channel found", extra={"channel_name": GENERAL_CHANNEL_NAME})
    if not _immune_channel_ids:
        log.warning("No immune channel found", extra={"channel_name": IMMUNE_CHANNEL_NAME})


@_bot.event
async def on_message(message: discord.Message):
    if message.author == _bot.user:
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

    payload = {
        "message_id": str(message.id),
        "channel_id": str(message.channel.id),
        "username": message.author.name,
        "content": content,
    }

    try:
        await _nc.publish(EARS_MESSAGE_IN, json.dumps(payload).encode())
        log.info("Published to NATS", extra={"subject": EARS_MESSAGE_IN})
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
            await message.remove_reaction(thinking_emoji, _bot.user)
        except Exception:
            pass


async def _handle_immune_command(message: discord.Message, content: str):
    """Handle messages in #maki-immune — forward to immune as direct commands."""
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


async def _response_listener():
    """Subscribe to NATS for outgoing responses and push chunks into queues."""
    sub = await _nc.subscribe(EARS_MESSAGE_OUT)
    log.info("Subscribed", extra={"subject": EARS_MESSAGE_OUT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            message_id = data.get("message_id", "")

            if _pending.push(message_id, data):
                log.info(
                    "Response chunk pushed",
                    extra={"message_id": message_id, "done": data.get("done", False)},
                )
            else:
                log.warning("Response for unknown message", extra={"message_id": message_id})
        except Exception:
            log.exception("Error processing NATS response")


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


async def _thought_listener():
    """Subscribe to NATS for proactive thoughts and post to #maki-thoughts."""
    sub = await _nc.subscribe(EARS_THOUGHT_OUT)
    log.info("Subscribed", extra={"subject": EARS_THOUGHT_OUT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            thought = data.get("thought", "")
            turn_id = data.get("turn_id", "unknown")

            if not thought:
                continue

            log.info("Thought received", extra={"turn_id": turn_id, "thought_len": len(thought)})

            for channel_id in _thoughts_channel_ids:
                channel = _bot.get_channel(channel_id)
                if channel:
                    await _send_response(channel, thought)
                    log.info("Thought posted", extra={"channel": THOUGHTS_CHANNEL_NAME, "channel_id": channel_id})

            if not _thoughts_channel_ids:
                log.warning("No thoughts channel available, thought dropped", extra={"turn_id": turn_id})

        except Exception:
            log.exception("Error processing thought")


async def _vitals_listener():
    """Subscribe to NATS for health digests and post to #maki-vitals."""
    sub = await _nc.subscribe(EARS_VITALS_OUT)
    log.info("Subscribed", extra={"subject": EARS_VITALS_OUT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            digest = data.get("digest", "")
            if not digest:
                continue

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


async def _alert_listener():
    """Subscribe to immune alerts and post to #maki-vitals."""
    sub = await _nc.subscribe(IMMUNE_ALERT)
    log.info("Subscribed", extra={"subject": IMMUNE_ALERT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            alert = data.get("alert", "")
            if not alert:
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
        except Exception:
            log.exception("Error processing alert")


async def _reminder_listener():
    """Subscribe to NATS for care reminders and post to #maki-reminders."""
    sub = await _nc.subscribe(EARS_REMINDER_OUT)
    log.info("Subscribed", extra={"subject": EARS_REMINDER_OUT})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            reminder = data.get("reminder", "")
            turn_id = data.get("turn_id", "unknown")

            if not reminder:
                continue

            log.info("Reminder received", extra={"turn_id": turn_id, "reminder_len": len(reminder)})

            for channel_id in _reminders_channel_ids:
                channel = _bot.get_channel(channel_id)
                if channel:
                    await _send_response(channel, reminder)
                    log.info(
                        "Reminder posted",
                        extra={"channel": REMINDERS_CHANNEL_NAME, "channel_id": channel_id},
                    )

            if not _reminders_channel_ids:
                log.warning("No reminders channel available, reminder dropped", extra={"turn_id": turn_id})

        except Exception:
            log.exception("Error processing reminder")


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


async def main():
    global _nc

    log.info("maki-ears starting", extra={"nats_url": NATS_URL})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)

    asyncio.create_task(_response_listener())
    asyncio.create_task(_immune_response_listener())
    asyncio.create_task(_thought_listener())
    asyncio.create_task(_vitals_listener())
    asyncio.create_task(_alert_listener())
    asyncio.create_task(_reminder_listener())

    try:
        await _bot.start(DISCORD_TOKEN)
    finally:
        await _nc.close()
        log.info("NATS connection closed")


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
