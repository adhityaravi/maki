"""Discord message routing.

Fans EARS_IN messages out as independent background tasks so a slow turn
doesn't block the next Discord message. Handles the ``!loop <name>``
control command inline and everything else through
:func:`cortex_io.process_turn` under a shared semaphore.
"""

from __future__ import annotations

import json
import logging

from maki_common import spawn_background, subscribe_supervised
from maki_common.subjects import EARS_IN, EARS_OUT

from maki_stem.cortex_io import process_turn, turn_semaphore
from maki_stem.loop_runner import trigger_loop
from maki_stem.loops import LoopSpec, StemContext

log = logging.getLogger(__name__)


async def _handle_discord_message(
    ctx: StemContext,
    loop_specs: list[LoopSpec],
    default_identity: str,
    health_endpoints: dict[str, str],
    conversation_history_size_fn,
    data: dict,
) -> None:
    """Handle a single Discord message (runs as independent task)."""
    channel_id = data.get("channel_id", "")
    message_id = data.get("message_id", "")
    content = data.get("content", "")
    forward_to = {"message_id": message_id, "channel_id": channel_id}

    # !loop <name> — manually trigger a named loop
    if content.strip().startswith("!loop "):
        loop_name = content.strip().removeprefix("!loop ").strip()
        await trigger_loop(ctx, loop_specs, loop_name, forward_to)
        return

    async with turn_semaphore:
        try:
            await process_turn(
                ctx,
                content,
                default_identity=default_identity,
                health_endpoints=health_endpoints,
                conversation_history_size=conversation_history_size_fn(),
                forward_to=forward_to,
            )
        except TimeoutError:
            error_msg = {
                "message_id": message_id,
                "channel_id": channel_id,
                "response": "Sorry, I took too long thinking about that. Try again?",
                "done": True,
            }
            try:
                await ctx.nc.publish(EARS_OUT, json.dumps(error_msg).encode())
            except Exception:
                log.exception("Failed to send timeout error to ears")
        except RuntimeError as e:
            log.warning("Turn cancelled", extra={"error": str(e)})
            try:
                error_msg = {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "response": "I lost my train of thought (my brain restarted). What were you saying?",
                    "done": True,
                }
                await ctx.nc.publish(EARS_OUT, json.dumps(error_msg).encode())
            except Exception:
                log.exception("Failed to send cancellation error to ears")
        except Exception:
            log.exception("Turn failed")
            try:
                error_msg = {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "response": "",
                    "done": True,
                }
                await ctx.nc.publish(EARS_OUT, json.dumps(error_msg).encode())
            except Exception:
                log.exception("Failed to send error to ears")


async def ears_listener(
    ctx: StemContext,
    loop_specs: list[LoopSpec],
    *,
    default_identity: str,
    health_endpoints: dict[str, str],
    conversation_history_size_fn,
    queue: str,
) -> None:
    """Listen for incoming Discord messages via NATS and dispatch as tasks.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent Discord
    message (issue #175).
    """

    async def handler(msg) -> None:
        try:
            data = json.loads(msg.data.decode())
            username = data.get("username", "unknown")
            log.info("Discord message", extra={"username": username, "content_len": len(data.get("content", ""))})
            spawn_background(
                _handle_discord_message(
                    ctx,
                    loop_specs,
                    default_identity,
                    health_endpoints,
                    conversation_history_size_fn,
                    data,
                ),
                name="stem.handle_discord_message",
            )
        except Exception:
            log.exception("Error dispatching Discord message")

    await subscribe_supervised(
        ctx.nc,
        EARS_IN,
        handler,
        queue=queue,
        # Dispatch-only: JSON decode + spawn_background. Ten seconds catches
        # a wedge in the dispatch path without pretending this handler runs
        # the actual turn (#492).
        handler_timeout=10.0,
        name="stem.ears_in",
    )
