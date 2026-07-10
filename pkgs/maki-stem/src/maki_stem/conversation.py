"""Conversation stream: init, cross-pod sync, publish, and history helpers.

Owns the module-level ``_history`` list — the ordered turn log every stem
pod maintains in sync via JetStream. On boot, ``init_conversation_stream``
replays the latest ``STREAM_MAX_MSGS`` turns; while running,
``conversation_sync_listener`` mirrors turns published by peers into the
same list so every pod sees a consistent view.

Design note: the history is module-level rather than passed around because
every module that touches conversation history conceptually shares one
canonical log — a StemState dataclass would just add a layer of indirection
without changing lifetime or ownership.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import nats.js.api
from maki_common import subscribe_supervised
from maki_common.subjects import CONVERSATION_STREAM
from nats.js.api import RetentionPolicy, StorageType

log = logging.getLogger(__name__)

STREAM_NAME = "maki-conversation"
STREAM_MAX_MSGS = int(os.environ.get("STREAM_MAX_MSGS", "200"))
CONTEXT_TURNS = int(os.environ.get("CONTEXT_TURNS", "15"))

# The canonical conversation log for this pod. Mutated by
# ``init_conversation_stream`` (initial replay), ``_handle_conversation_sync``
# (peer turns), and ``publish_turn_to_stream`` (local turns).
_history: list[dict] = []


def history_size() -> int:
    """Return the number of turns currently held in memory."""
    return len(_history)


async def init_conversation_stream(js) -> None:
    """Create or connect to the conversation stream and load existing history."""
    try:
        await js.find_stream_name_by_subject(CONVERSATION_STREAM)
        log.info("Conversation stream exists", extra={"stream": STREAM_NAME})
    except Exception:
        await js.add_stream(
            name=STREAM_NAME,
            subjects=[CONVERSATION_STREAM],
            retention=RetentionPolicy.LIMITS,
            max_msgs=STREAM_MAX_MSGS,
            storage=StorageType.FILE,
        )
        log.info("Created conversation stream", extra={"stream": STREAM_NAME, "max_msgs": STREAM_MAX_MSGS})

    try:
        sub = await js.subscribe(CONVERSATION_STREAM, ordered_consumer=True)
        while True:
            try:
                msg = await sub.next_msg(timeout=1.0)
                turn_doc = json.loads(msg.data.decode())
                _history.append(turn_doc)
            except TimeoutError:
                break
        await sub.unsubscribe()
        log.info("Loaded conversation history", extra={"turns": len(_history)})
    except Exception:
        log.exception("Error loading conversation history")
        log.info("Starting with empty conversation history")


async def _handle_conversation_sync(msg) -> None:
    """Sync one conversation turn into ``_history``.

    ``subscribe_supervised`` handles JS acking (auto_ack=True) — ACK on
    success, NAK on uncaught handler exception so JS redelivers (issue
    #221). This handler swallows all exceptions internally, so failures
    are effectively fire-and-forget today; rework the broad ``try/except``
    below if at-least-once delivery becomes load-bearing.
    """
    try:
        turn_doc = json.loads(msg.data.decode())
        turn_id = turn_doc.get("turn_id", "")

        # Skip if we already have this turn (we added it locally in publish_turn_to_stream)
        if any(t.get("turn_id") == turn_id for t in _history[-50:]):
            return

        _history.append(turn_doc)

        # Keep bounded
        while len(_history) > STREAM_MAX_MSGS:
            _history.pop(0)

        log.info(
            "Conversation synced from stream",
            extra={"turn_id": turn_id, "instance": turn_doc.get("instance_id", "?")},
        )
    except Exception:
        log.exception("Error syncing conversation")


async def conversation_sync_listener(nc, js, instance_id: str) -> None:
    """Live subscriber to conversation stream — keeps ``_history`` in sync.

    Ensures that all stem instances see turns processed by any instance.
    Uses a durable push consumer so we don't miss messages while running.

    Wrapped in ``subscribe_supervised`` so a JS reconnect / stream drain
    re-subscribes instead of silently freezing the conversation history
    (issue #175).
    """
    # Use deliver_last_per_subject to start from where we left off (after startup replay).
    # Subscribe with a unique durable name per instance to get independent delivery.
    consumer_name = f"stem-sync-{instance_id}"
    log.info("Conversation sync listener started", extra={"instance_id": instance_id})
    await subscribe_supervised(
        nc,
        CONVERSATION_STREAM,
        _handle_conversation_sync,
        js=js,
        durable=consumer_name,
        deliver_policy=nats.js.api.DeliverPolicy.LAST_PER_SUBJECT,
        name="stem.conversation_sync",
    )


async def publish_turn_to_stream(
    js,
    instance_id: str,
    turn_id: str,
    user_message: str,
    cortex_response: str,
) -> None:
    """Publish completed turn to conversation stream and update in-memory history."""
    turn_doc = {
        "timestamp": datetime.now(UTC).isoformat(),
        "turn_id": turn_id,
        "user_message": user_message,
        "cortex_response": cortex_response,
        "instance_id": instance_id,
        "memories_used": [],
        "mission_proposed": None,
    }

    try:
        ack = await js.publish(CONVERSATION_STREAM, json.dumps(turn_doc).encode())
        _history.append(turn_doc)
        log.info("Turn published to stream", extra={"turn_id": turn_id, "seq": ack.seq})
    except Exception:
        log.exception("Failed to publish turn to stream", extra={"turn_id": turn_id})


def get_recent_conversation() -> list[dict]:
    """Get recent conversation history formatted for cortex."""
    recent = _history[-CONTEXT_TURNS:]
    conversation = []
    for turn_doc in recent:
        conversation.append(
            {
                "role": "user",
                "content": turn_doc["user_message"],
                "timestamp": turn_doc["timestamp"],
            }
        )
        conversation.append(
            {
                "role": "assistant",
                "content": turn_doc["cortex_response"],
                "timestamp": turn_doc["timestamp"],
            }
        )
    return conversation


def build_session_summary() -> str:
    """Build a compact summary of turns that fall outside the recent context window.

    These are turns older than CONTEXT_TURNS that won't appear in get_recent_conversation().
    Gives cortex awareness of earlier parts of the same session without bloating the full context.
    """
    if len(_history) <= CONTEXT_TURNS:
        return ""

    older_turns = _history[:-CONTEXT_TURNS]
    if not older_turns:
        return ""

    lines = [f"Earlier in this session ({len(older_turns)} turns before recent context):"]
    for turn in older_turns:
        user_msg = turn.get("user_message", "")[:120].replace("\n", " ").strip()
        if user_msg:
            lines.append(f"- {user_msg}")

    return "\n".join(lines)
