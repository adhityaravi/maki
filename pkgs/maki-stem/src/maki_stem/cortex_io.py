"""Cortex coordination: response listener, heartbeat watcher, and process_turn.

Owns the module-level state that tracks cortex's liveness and the turns
this pod has in flight:

- ``_cortex_sessions``: instance_id → session_id, updated by the heartbeat
  watcher; when a session_id changes we know cortex restarted mid-turn and
  cancel every pending turn on this pod instead of waiting for
  ``TURN_TIMEOUT``.
- ``active_turns``: turn_id → wall-clock start, exposed to the readiness
  probe so a wedged turn flips the pod's ``/health`` to red.
- ``turn_semaphore``: caps concurrent chat turns from Discord to keep
  cortex from getting hammered.

``process_turn`` is the single turn skeleton — used by both the HTTP
``/turn`` endpoint and the Discord relay. It pulls identity, memories,
system state, config, and recent conversation, then streams via
``submit_turn_streaming`` (which handles CORTEX_STUCK on timeout uniformly
across every call site — see #125).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from maki_common import (
    kv_put_float,
    load_kv_config,
    parse_config_tags,
    spawn_background,
    strip_tags,
    subscribe_supervised,
)
from maki_common.config import apply_config_updates
from maki_common.subjects import (
    CORTEX_HEALTH,
    CORTEX_TURN_RESPONSE,
    EARS_OUT,
)

from maki_stem.conversation import (
    build_session_summary,
    get_recent_conversation,
    publish_turn_to_stream,
)
from maki_stem.loops import StemContext
from maki_stem.memory import feed_memories, search_memories
from maki_stem.system_state import (
    gather_system_state,
    is_health_query,
    summarize_system_state,
)
from maki_stem.turn import new_turn_id, submit_turn_streaming

log = logging.getLogger(__name__)

TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "1800"))

KV_KEY = "identity"

# instance_id → session_id (session_id changes when cortex restarts mid-turn).
_cortex_sessions: dict[str, str] = {}

# turn_id → cortex instance_id owning the turn. Populated by the response
# listener on the first chunk we see for a turn (every CORTEX_TURN_RESPONSE
# chunk carries instance_id — issue #394). Consulted by the heartbeat watcher
# to cancel ONLY the turns owned by a restarted cortex instance instead of
# nuking every in-flight turn on the fleet. Entries are dropped when the
# owning turn's done chunk arrives, or (belt-and-suspenders) by process_turn's
# finally block.
_turn_to_instance: dict[str, str] = {}

# turn_id → wall-clock start. Read by the readiness probe in main.
active_turns: dict[str, float] = {}

# Cap concurrent chat turns to protect cortex.
turn_semaphore = asyncio.Semaphore(2)


async def _handle_response(msg, pending) -> None:
    """Push one cortex response chunk into the pending queue.

    Also records ``turn_id → instance_id`` so the heartbeat watcher can
    selectively cancel only the turns owned by a restarted cortex instance
    (issue #394). The mapping is cleaned up on the terminal ``done`` chunk;
    ``process_turn`` also pops it in ``finally`` in case done never arrives.
    """
    try:
        data = json.loads(msg.data.decode())
        turn_id = data.get("turn_id")
        instance_id = data.get("instance_id")
        done = data.get("done", False)
        if turn_id and pending.push(turn_id, data):
            if instance_id:
                _turn_to_instance[turn_id] = instance_id
            log.info(
                "Response chunk pushed",
                extra={"turn_id": turn_id, "done": done, "instance_id": instance_id},
            )
            if done:
                _turn_to_instance.pop(turn_id, None)
        else:
            log.warning("Response for unknown turn", extra={"turn_id": turn_id})
    except Exception:
        log.exception("Error processing cortex response")


async def response_listener(nc, pending) -> None:
    """Listen for cortex responses and push chunks into pending queues.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of leaving every pending turn hanging until
    ``TURN_TIMEOUT`` (issue #175). Broadcast: every pod tracks its own
    pendings, so each pod must see every response chunk — no queue group.
    """

    async def handler(msg):
        await _handle_response(msg, pending)

    await subscribe_supervised(
        nc,
        CORTEX_TURN_RESPONSE,
        handler,
        name="stem.cortex_response",
    )


async def _handle_cortex_heartbeat(msg, pending) -> None:
    """Track cortex session changes and cancel stale turns on restart."""
    try:
        payload = json.loads(msg.data.decode())
        session_id = payload.get("session_id")
        if not session_id:
            return

        instance_id = payload.get("instance_id", session_id)

        if instance_id not in _cortex_sessions:
            _cortex_sessions[instance_id] = session_id
            log.info("Cortex session tracked", extra={"instance_id": instance_id, "session_id": session_id})
            return

        old_session = _cortex_sessions[instance_id]
        if session_id != old_session:
            _cortex_sessions[instance_id] = session_id
            pending_keys = pending.pending_keys()
            if not pending_keys:
                log.info(
                    "Cortex instance restarted (no pending turns)",
                    extra={"instance_id": instance_id, "old_session": old_session, "new_session": session_id},
                )
                return

            # Selective cancellation (issue #394): the fleet runs multiple
            # cortex replicas load-balanced via NATS queue group, so a
            # restart on instance A must NOT nuke turns being streamed by
            # healthy instance B. Split pending turns into three buckets:
            #
            #   - owned:    turn_id → this instance_id (must cancel)
            #   - other:    turn_id → some other instance_id (leave alone)
            #   - unmapped: no first chunk yet, so ownership unknown
            #
            # For the unmapped bucket we cancel too — stem can't prove the
            # turn isn't on the restarted instance, and leaving it hanging
            # for TURN_TIMEOUT (30 min) is the worse failure mode. Logged
            # distinctly so the "false-cancel unmapped turn" case is
            # visible in metrics/logs if it starts happening at scale.
            owned: list[str] = []
            other: list[str] = []
            unmapped: list[str] = []
            for turn_id in pending_keys:
                mapped = _turn_to_instance.get(turn_id)
                if mapped is None:
                    unmapped.append(turn_id)
                elif mapped == instance_id:
                    owned.append(turn_id)
                else:
                    other.append(turn_id)

            to_cancel = owned + unmapped
            cancelled = pending.cancel_keys(to_cancel)
            for turn_id in to_cancel:
                _turn_to_instance.pop(turn_id, None)

            log.warning(
                "Cortex restarted — cancelled owned/unmapped turns",
                extra={
                    "instance_id": instance_id,
                    "old_session": old_session,
                    "new_session": session_id,
                    "cancelled_turns": cancelled,
                    "owned_turn_ids": owned,
                    "unmapped_turn_ids": unmapped,
                    "other_instance_turn_ids": other,
                },
            )
    except Exception:
        log.exception("Error in cortex heartbeat watcher")


async def cortex_heartbeat_watcher(nc, pending) -> None:
    """Watch cortex heartbeat for session_id changes (restarts).

    When cortex restarts mid-turn, its session_id changes. We detect this
    and cancel all pending turns immediately instead of waiting 30 minutes
    for the timeout. Tracks sessions per instance_id to support
    multi-instance cortex.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of leaving ``_cortex_sessions`` frozen and
    health-keyword queries silently returning stale state (issue #175).
    Broadcast: each stem pod tracks cortex liveness independently — no
    queue group.
    """

    async def handler(msg):
        await _handle_cortex_heartbeat(msg, pending)

    await subscribe_supervised(
        nc,
        CORTEX_HEALTH,
        handler,
        name="stem.cortex_health",
    )


async def process_turn(
    ctx: StemContext,
    message: str,
    *,
    default_identity: str,
    health_endpoints: dict[str, str],
    conversation_history_size: int,
    forward_to: dict | None = None,
) -> tuple[str, str]:
    """Core turn logic with streaming. Returns (turn_id, full_response_text).

    If ``forward_to`` is provided (dict with message_id, channel_id),
    streams each chunk to EARS_OUT as it arrives from cortex.
    """
    await kv_put_float(ctx.lock_kv, "stem.last_activity", time.time())

    turn_id = new_turn_id("turn")
    active_turns[turn_id] = time.time()
    log.info("Turn started", extra={"turn_id": turn_id, "message_len": len(message)})

    try:
        entry = await ctx.kv.get(KV_KEY)
        identity = entry.value.decode()
    except Exception:
        identity = default_identity

    memories, graph_context = await search_memories(message)
    system_state = await gather_system_state(
        ctx.nc,
        conversation_history_size=conversation_history_size,
        health_endpoints=health_endpoints,
    )

    # Include full system state only for health-related queries; otherwise send a summary
    if is_health_query(message):
        turn_system_state: dict | None = system_state
        turn_system_state_summary: str | None = None
    else:
        turn_system_state = None
        turn_system_state_summary = summarize_system_state(system_state)

    config = await load_kv_config(ctx.config_kv, ctx.default_config)
    chat_model = config.get("chat_model", "")

    turn_payload = {
        "turn_id": turn_id,
        "identity": identity,
        "conversation": get_recent_conversation(),
        "session_summary": build_session_summary(),
        "memories": memories,
        "graph_context": graph_context,
        "system_state": turn_system_state,
        "system_state_summary": turn_system_state_summary,
        "prompt": message,
        **({"model": chat_model} if chat_model else {}),
    }

    full_response: list[str] = []

    try:
        log.info("Turn request publishing", extra={"turn_id": turn_id})
        # ``submit_turn_streaming`` handles publish + queue lifecycle and
        # publishes CORTEX_STUCK on timeout uniformly across all callers (#125).
        async for data in submit_turn_streaming(
            ctx,
            turn_id=turn_id,
            payload=turn_payload,
            timeout=TURN_TIMEOUT,
            mode="normal",
            user_waiting=True,
        ):
            if data.get("cancelled"):
                log.warning(
                    "Turn cancelled by cortex restart",
                    extra={"turn_id": turn_id, "partial_chunks": len(full_response)},
                )
                # Raise so the caller's RuntimeError handler runs:
                # no memory write, no conversation stream publish, and ears
                # receives the "lost my train of thought" message instead of
                # a phantom empty done chunk.
                raise RuntimeError("cortex_restart_cancelled")

            chunk_text = data.get("response", "")
            done = data.get("done", False)

            if chunk_text:
                full_response.append(chunk_text)

            if forward_to and (chunk_text or done):
                ears_msg = {
                    "message_id": forward_to["message_id"],
                    "channel_id": forward_to["channel_id"],
                    "turn_id": turn_id,
                    "response": chunk_text,
                    "done": done,
                }
                await ctx.nc.publish(EARS_OUT, json.dumps(ears_msg).encode())

        cortex_response = "".join(full_response)
        clean_response = strip_tags(cortex_response)
        config_updates = parse_config_tags(cortex_response)
        if config_updates:
            await apply_config_updates(ctx.config_kv, config_updates, allowed_keys=set(ctx.default_config.keys()))

        spawn_background(
            publish_turn_to_stream(ctx.js, ctx.instance_id, turn_id, message, clean_response),
            name="stem.publish_turn_to_stream",
        )
        spawn_background(feed_memories(message, clean_response), name="stem.feed_memories")

        return turn_id, clean_response

    except TimeoutError:
        log.error("Turn timed out", extra={"turn_id": turn_id})
        raise
    finally:
        active_turns.pop(turn_id, None)
        # Belt-and-suspenders: _handle_response drops the mapping on the
        # done chunk, but pop again here so a turn that never received a
        # terminal chunk (TimeoutError, TurnPublishError, RuntimeError from
        # cortex_restart_cancelled, etc.) doesn't leak into
        # _turn_to_instance forever (issue #394).
        _turn_to_instance.pop(turn_id, None)
