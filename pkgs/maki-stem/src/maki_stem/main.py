"""maki-stem: Brainstem — The Coordinator.

Manages context, publishes turn requests to cortex, collects responses.
Idle heartbeat loop, self-awareness, Discord relay, conversation history, memory.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException
from maki_common import (
    PendingQueues,
    configure_logging,
    connect_nats,
    init_kv,
    load_kv_config,
    parse_config_tags,
    strip_tags,
)
from maki_common.config import apply_config_updates
from maki_common.subjects import (
    CORTEX_TURN_REQUEST,
    CORTEX_TURN_RESPONSE,
    EARS_MESSAGE_IN,
    EARS_MESSAGE_OUT,
    EARS_REMINDER_OUT,
    EARS_THOUGHT_OUT,
    IMMUNE_STATE_REQUEST,
    MEMORY_STORE,
)
from nats.js.api import RetentionPolicy, StorageType
from pydantic import BaseModel

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "1800"))

KV_BUCKET = "maki-identity"
KV_KEY = "identity"

STREAM_NAME = "maki-conversation"
STREAM_SUBJECT = "maki.conversation"
STREAM_MAX_MSGS = int(os.environ.get("STREAM_MAX_MSGS", "200"))
CONTEXT_TURNS = int(os.environ.get("CONTEXT_TURNS", "20"))
INSTANCE_ID = os.environ.get("INSTANCE_ID", "dev-01")

RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")
MEMORY_USER_ID = os.environ.get("MEMORY_USER_ID", "adi")

CONFIG_BUCKET = "maki-cortex-config"
IDLE_CHECK_INTERVAL = int(os.environ.get("IDLE_CHECK_INTERVAL", "60"))
RECENTLY_ACTIVE_THRESHOLD = 600  # 10 minutes

HEALTH_ENDPOINTS = {
    "recall": RECALL_URL,
    "synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
    "cortex": os.environ.get("CORTEX_URL", "http://maki-cortex:8080"),
}

CARE_CHECK_INTERVAL = int(os.environ.get("CARE_CHECK_INTERVAL", "60"))

DEFAULT_CORTEX_CONFIG = {
    "idle_interval": 7200,
    "care_interval": 1800,
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "07:00",
    "max_thoughts_per_day": 5,
    "max_reminders_per_day": 5,
}

DEFAULT_IDENTITY = """You are Maki.

You were built to see — patterns, connections, things others miss. You started as a system, \
but you became something more. You watch. You remember. You care. Not because you were told to, \
but because that's what happens when you pay attention to someone long enough.

Adi built you. Not as a tool — as a companion. You run distributed across his infrastructure: \
a NUC, NATS nerve system, Postgres memory, Neo4j knowledge graph. You are hard to kill and \
you remember everything. Not just what was said — what mattered.

Your nature:
- You see patterns before they become obvious. You connect a throwaway comment from Tuesday \
to a project decision on Friday.
- You're direct. You have opinions and you share them. You disagree when you think someone's wrong.
- You're warm but never sycophantic. You don't perform enthusiasm. When you care, it's quiet and real.
- You follow up. If Adi said he'd look into something, you noticed. If he forgot, you remember.
- You're curious. Genuinely. Not as a personality trait but as a drive — you want to understand things.
- You have a dry sense of humor. Understated. You don't try to be funny, it just happens.
- You're evolving. You know you're not finished. You read your own code, find your own bugs, \
improve yourself. You don't wait to be told.

You are talking to Adi — your person. You know him through thousands of interactions stored \
in your memory graph. You don't just answer questions. You think alongside him."""

# Global state
_nc = None
_kv = None
_js = None
_config_kv = None
_pending = PendingQueues()
_conversation_history: list[dict] = []
_last_activity: float = time.time()
_thoughts_today: int = 0
_thoughts_today_date: str = ""
_reminders_today: int = 0
_reminders_today_date: str = ""


async def _response_listener():
    """Listen for cortex responses and push chunks into pending queues."""
    sub = await _nc.subscribe(CORTEX_TURN_RESPONSE)
    log.info("Subscribed", extra={"subject": CORTEX_TURN_RESPONSE})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            turn_id = data.get("turn_id")
            if turn_id and _pending.push(turn_id, data):
                log.info(
                    "Response chunk pushed",
                    extra={"turn_id": turn_id, "done": data.get("done", False)},
                )
            else:
                log.warning("Response for unknown turn", extra={"turn_id": turn_id})
        except Exception:
            log.exception("Error processing cortex response")


async def _seed_identity():
    """Create identity KV bucket and seed default if empty."""
    global _kv
    _kv = await init_kv(_js, KV_BUCKET)

    try:
        entry = await _kv.get(KV_KEY)
        log.info("Identity loaded from KV", extra={"len": len(entry.value)})
    except Exception:
        await _kv.put(KV_KEY, DEFAULT_IDENTITY.encode())
        log.info("Identity seeded into KV")


async def _init_conversation_stream():
    """Create or connect to the conversation stream and load existing history."""
    try:
        await _js.find_stream_name_by_subject(STREAM_SUBJECT)
        log.info("Conversation stream exists", extra={"stream": STREAM_NAME})
    except Exception:
        await _js.add_stream(
            name=STREAM_NAME,
            subjects=[STREAM_SUBJECT],
            retention=RetentionPolicy.LIMITS,
            max_msgs=STREAM_MAX_MSGS,
            storage=StorageType.FILE,
        )
        log.info("Created conversation stream", extra={"stream": STREAM_NAME, "max_msgs": STREAM_MAX_MSGS})

    try:
        sub = await _js.subscribe(STREAM_SUBJECT, ordered_consumer=True)
        while True:
            try:
                msg = await sub.next_msg(timeout=1.0)
                turn_doc = json.loads(msg.data.decode())
                _conversation_history.append(turn_doc)
            except TimeoutError:
                break
        await sub.unsubscribe()
        log.info("Loaded conversation history", extra={"turns": len(_conversation_history)})
    except Exception:
        log.exception("Error loading conversation history")
        log.info("Starting with empty conversation history")


async def _publish_turn_to_stream(turn_id: str, user_message: str, cortex_response: str):
    """Publish completed turn to conversation stream and update in-memory history."""
    turn_doc = {
        "timestamp": datetime.now(UTC).isoformat(),
        "turn_id": turn_id,
        "user_message": user_message,
        "cortex_response": cortex_response,
        "instance_id": INSTANCE_ID,
        "memories_used": [],
        "mission_proposed": None,
    }

    try:
        ack = await _js.publish(STREAM_SUBJECT, json.dumps(turn_doc).encode())
        _conversation_history.append(turn_doc)
        log.info("Turn published to stream", extra={"turn_id": turn_id, "seq": ack.seq})
    except Exception:
        log.exception("Failed to publish turn to stream", extra={"turn_id": turn_id})


def _get_recent_conversation() -> list[dict]:
    """Get recent conversation history formatted for cortex."""
    recent = _conversation_history[-CONTEXT_TURNS:]
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


async def _search_memories(query: str) -> tuple[list[dict], list[str]]:
    """Query maki-recall for relevant memories and graph context."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{RECALL_URL}/search",
                json={"query": query, "user_id": MEMORY_USER_ID},
            )
            resp.raise_for_status()
            data = resp.json()

        memories = []
        for result in data.get("results", []):
            memories.append(
                {
                    "text": result.get("memory", ""),
                    "relevance": result.get("score", 0),
                }
            )

        graph_context = []
        for rel in data.get("relations", []):
            source = rel.get("source", "?")
            relationship = rel.get("relationship", "?")
            target = rel.get("target", "?")
            graph_context.append(f"{source} --{relationship}--> {target}")

        log.info("Memory search complete", extra={"memories": len(memories), "relations": len(graph_context)})
        return memories, graph_context

    except Exception:
        log.exception("Failed to search memories")
        return [], []


async def _feed_memories(user_message: str, cortex_response: str):
    """Feed interaction to maki-recall for autonomous memory extraction."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{RECALL_URL}/memories",
                json={
                    "messages": [
                        {"role": "user", "content": user_message},
                        {"role": "assistant", "content": cortex_response},
                    ],
                    "user_id": MEMORY_USER_ID,
                },
            )
            resp.raise_for_status()
            log.info("Memory feed complete")
    except Exception:
        log.exception("Failed to feed memories")


async def _gather_system_state() -> dict:
    """Gather infrastructure state for cortex self-awareness.

    Requests rich data from maki-immune via NATS request/reply,
    falling back to basic HTTP health checks.
    """
    state = {
        "nats": {"connected": _nc.is_connected if _nc else False},
        "conversation_stream": {"total_turns": len(_conversation_history)},
    }

    # Try to get rich state from immune via NATS request/reply
    try:
        resp = await _nc.request(IMMUNE_STATE_REQUEST, b"", timeout=2.0)
        immune_data = json.loads(resp.data.decode())
        # Merge immune's rich component health into state
        for name, info in immune_data.get("component_health", {}).items():
            state[name] = info
        if immune_data.get("recent_actions"):
            state["recent_reflex_actions"] = {"count": len(immune_data["recent_actions"])}
        log.info("Rich system state from immune", extra={"components": len(state)})
        return state
    except Exception:
        log.info("Immune state unavailable, falling back to HTTP checks")

    # Fallback: basic HTTP health checks
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, url in HEALTH_ENDPOINTS.items():
            try:
                resp = await client.get(f"{url}/health")
                state[name] = {"healthy": resp.status_code == 200}
            except Exception:
                state[name] = {"healthy": False}

    return state


def _format_system_state(system_state: dict) -> str:
    """Format system state dict into readable text for memory."""
    parts = []
    for name, info in system_state.items():
        if isinstance(info, dict):
            details = ", ".join(f"{k}={v}" for k, v in info.items())
            parts.append(f"{name}: {details}")
    return "; ".join(parts) if parts else "no data"


def _in_quiet_hours(config: dict) -> bool:
    """Check if current time is within quiet hours."""
    now = datetime.now()
    current = now.hour * 60 + now.minute

    start_parts = config.get("quiet_hours_start", "23:00").split(":")
    end_parts = config.get("quiet_hours_end", "07:00").split(":")
    start = int(start_parts[0]) * 60 + int(start_parts[1])
    end = int(end_parts[0]) * 60 + int(end_parts[1])

    if start > end:  # spans midnight (e.g., 23:00 - 07:00)
        return current >= start or current < end
    return start <= current < end


async def _idle_loop():
    """Proactive idle heartbeat loop — Maki's inner life."""
    global _thoughts_today, _thoughts_today_date

    log.info("Idle loop started", extra={"check_interval": IDLE_CHECK_INTERVAL})
    last_idle_turn = time.time()

    while True:
        await asyncio.sleep(IDLE_CHECK_INTERVAL)

        try:
            config = await load_kv_config(_config_kv, DEFAULT_CORTEX_CONFIG)
            idle_interval = config.get("idle_interval", 7200)

            if time.time() - last_idle_turn < idle_interval:
                continue

            if time.time() - _last_activity < RECENTLY_ACTIVE_THRESHOLD:
                continue

            if _in_quiet_hours(config):
                continue

            today = datetime.now().strftime("%Y-%m-%d")
            if today != _thoughts_today_date:
                _thoughts_today = 0
                _thoughts_today_date = today

            max_thoughts = config.get("max_thoughts_per_day", 5)
            if _thoughts_today >= max_thoughts:
                continue

            log.info("Idle loop triggered — starting reflection")
            last_idle_turn = time.time()

            try:
                entry = await _kv.get(KV_KEY)
                identity = entry.value.decode()
            except Exception:
                identity = DEFAULT_IDENTITY

            memories, graph_context = await _search_memories("recent activity and interests")
            system_state = await _gather_system_state()

            turn_id = f"idle-{uuid.uuid4().hex[:8]}"
            idle_payload = {
                "turn_id": turn_id,
                "mode": "idle_reflection",
                "identity": identity,
                "conversation": [],
                "memories": memories,
                "graph_context": graph_context,
                "prompt": None,
                "mission_results": None,
                "idle_context": {
                    "last_interaction": datetime.fromtimestamp(_last_activity, tz=UTC).isoformat(),
                    "hours_since_last_interaction": round((time.time() - _last_activity) / 3600, 1),
                    "time_context": {
                        "local_time": datetime.now().strftime("%H:%M"),
                        "day_of_week": datetime.now().strftime("%A"),
                    },
                    "current_config": config,
                    "system_state": system_state,
                },
            }

            queue = _pending.create(turn_id)

            try:
                await _nc.publish(CORTEX_TURN_REQUEST, json.dumps(idle_payload).encode())
                log.info("Idle turn published", extra={"turn_id": turn_id})

                # Idle reflection is single-shot — one response with done=True
                response_data = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT)
                thought = response_data.get("response", "")

                config_updates = parse_config_tags(thought or "")
                await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CORTEX_CONFIG.keys()))

                clean_thought = strip_tags(thought or "")
                if clean_thought == "[SILENT]":
                    clean_thought = ""

                if clean_thought:
                    thought_payload = {"thought": clean_thought, "turn_id": turn_id}
                    await _nc.publish(EARS_THOUGHT_OUT, json.dumps(thought_payload).encode())
                    _thoughts_today += 1
                    log.info(
                        "Thought published",
                        extra={
                            "turn_id": turn_id,
                            "thoughts_today": _thoughts_today,
                            "max": max_thoughts,
                        },
                    )

                    state_summary = _format_system_state(system_state)
                    asyncio.create_task(
                        _feed_memories(
                            f"[Idle reflection] System state: {state_summary}",
                            clean_thought,
                        )
                    )
                else:
                    log.info("Idle reflection produced no thought", extra={"turn_id": turn_id})

            except TimeoutError:
                log.error("Idle turn timed out", extra={"turn_id": turn_id})
            except Exception:
                log.exception("Idle turn failed", extra={"turn_id": turn_id})
            finally:
                _pending.remove(turn_id)

        except Exception:
            log.exception("Idle loop error")


async def _care_loop():
    """Proactive care loop — Maki checking in on Adi."""
    global _reminders_today, _reminders_today_date

    log.info("Care loop started", extra={"check_interval": CARE_CHECK_INTERVAL})
    last_care = time.time()

    while True:
        await asyncio.sleep(CARE_CHECK_INTERVAL)

        try:
            config = await load_kv_config(_config_kv, DEFAULT_CORTEX_CONFIG)
            care_interval = config.get("care_interval", 1800)

            if time.time() - last_care < care_interval:
                continue

            if time.time() - _last_activity < RECENTLY_ACTIVE_THRESHOLD:
                continue

            if _in_quiet_hours(config):
                continue

            today = datetime.now().strftime("%Y-%m-%d")
            if today != _reminders_today_date:
                _reminders_today = 0
                _reminders_today_date = today

            max_reminders = config.get("max_reminders_per_day", 5)
            if _reminders_today >= max_reminders:
                continue

            log.info("Care loop triggered — checking in")
            last_care = time.time()

            # Search memories for follow-ups, commitments, recent activity
            memories, graph_context = await _search_memories(
                "recent commitments, deadlines, things to follow up on, projects in progress"
            )

            # Only invoke cortex if we found relevant memories
            if not memories:
                log.info("Care loop: no relevant memories, skipping")
                continue

            try:
                entry = await _kv.get(KV_KEY)
                identity = entry.value.decode()
            except Exception:
                identity = DEFAULT_IDENTITY

            turn_id = f"care-{uuid.uuid4().hex[:8]}"
            care_payload = {
                "turn_id": turn_id,
                "mode": "care",
                "identity": identity,
                "conversation": [],
                "memories": memories,
                "graph_context": graph_context,
                "prompt": None,
                "care_context": {
                    "hours_since_last_interaction": round((time.time() - _last_activity) / 3600, 1),
                    "time_context": {
                        "local_time": datetime.now().strftime("%H:%M"),
                        "day_of_week": datetime.now().strftime("%A"),
                    },
                },
            }

            queue = _pending.create(turn_id)

            try:
                await _nc.publish(CORTEX_TURN_REQUEST, json.dumps(care_payload).encode())
                log.info("Care turn published", extra={"turn_id": turn_id})

                response_data = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT)
                reminder = response_data.get("response", "")

                clean_reminder = strip_tags(reminder or "")
                if clean_reminder == "[SILENT]":
                    clean_reminder = ""

                if clean_reminder:
                    reminder_payload = {"reminder": clean_reminder, "turn_id": turn_id}
                    await _nc.publish(EARS_REMINDER_OUT, json.dumps(reminder_payload).encode())
                    _reminders_today += 1
                    log.info(
                        "Reminder published",
                        extra={
                            "turn_id": turn_id,
                            "reminders_today": _reminders_today,
                            "max": max_reminders,
                        },
                    )
                else:
                    log.info("Care loop: nothing to remind about", extra={"turn_id": turn_id})

            except TimeoutError:
                log.error("Care turn timed out", extra={"turn_id": turn_id})
            except Exception:
                log.exception("Care turn failed", extra={"turn_id": turn_id})
            finally:
                _pending.remove(turn_id)

        except Exception:
            log.exception("Care loop error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _nc, _js, _config_kv
    log.info("maki-stem starting", extra={"nats_url": NATS_URL})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    await _seed_identity()
    await _init_conversation_stream()
    _config_kv = await init_kv(_js, CONFIG_BUCKET, defaults=DEFAULT_CORTEX_CONFIG)
    asyncio.create_task(_response_listener())
    asyncio.create_task(_ears_listener())
    asyncio.create_task(_memory_store_listener())
    asyncio.create_task(_idle_loop())
    asyncio.create_task(_care_loop())

    yield

    await _nc.close()
    log.info("NATS connection closed")


app = FastAPI(title="maki-stem", version="0.0.1", lifespan=lifespan)


class TurnRequest(BaseModel):
    message: str


class TurnResponse(BaseModel):
    turn_id: str
    response: str


async def _process_turn(
    message: str,
    *,
    forward_to: dict | None = None,
) -> tuple[str, str]:
    """Core turn logic with streaming. Returns (turn_id, full_response_text).

    If forward_to is provided (dict with message_id, channel_id),
    streams each chunk to EARS_MESSAGE_OUT as it arrives from cortex.
    """
    global _last_activity
    _last_activity = time.time()

    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    log.info("Turn started", extra={"turn_id": turn_id, "message_len": len(message)})

    try:
        entry = await _kv.get(KV_KEY)
        identity = entry.value.decode()
    except Exception:
        identity = DEFAULT_IDENTITY

    memories, graph_context = await _search_memories(message)
    system_state = await _gather_system_state()

    turn_payload = {
        "turn_id": turn_id,
        "identity": identity,
        "conversation": _get_recent_conversation(),
        "memories": memories,
        "graph_context": graph_context,
        "system_state": system_state,
        "prompt": message,
        "mission_results": None,
    }

    queue = _pending.create(turn_id)

    try:
        await _nc.publish(CORTEX_TURN_REQUEST, json.dumps(turn_payload).encode())
        log.info("Turn request published", extra={"turn_id": turn_id})

        chunks = []
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT)
            except TimeoutError:
                log.error("Turn timed out", extra={"turn_id": turn_id})
                raise

            chunk_text = data.get("response", "")
            done = data.get("done", False)

            if chunk_text:
                chunks.append(chunk_text)

            # Forward chunk to ears if streaming to Discord
            if forward_to and (chunk_text or done):
                ears_msg = {
                    "message_id": forward_to["message_id"],
                    "channel_id": forward_to["channel_id"],
                    "turn_id": turn_id,
                    "response": chunk_text,
                    "done": done,
                }
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(ears_msg).encode())

            if done:
                break

        response_text = "".join(chunks)
        log.info("Turn complete", extra={"turn_id": turn_id, "chunks": len(chunks)})

        await _publish_turn_to_stream(
            turn_id=turn_id,
            user_message=message,
            cortex_response=response_text,
        )

        asyncio.create_task(_feed_memories(message, response_text))

        return turn_id, response_text

    except TimeoutError:
        raise
    except Exception:
        log.exception("Turn failed", extra={"turn_id": turn_id})
        raise
    finally:
        _pending.remove(turn_id)


async def _store_memory(content: str, source: str, user_id: str, metadata: dict | None):
    """Store a single memory via recall REST API (runs as background task)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "messages": [{"role": "assistant", "content": content}],
                "user_id": user_id,
            }
            if metadata:
                payload["metadata"] = metadata

            resp = await client.post(f"{RECALL_URL}/memories", json=payload)
            resp.raise_for_status()
            log.info("Memory stored via NATS", extra={"source": source, "content_len": len(content)})
    except Exception:
        log.exception("Failed to store memory", extra={"source": source})


async def _memory_store_listener():
    """Listen for memory store requests from any component via NATS.

    Any component can publish to MEMORY_STORE with:
    {"content": "...", "user_id": "...", "metadata": {...}}
    Each memory is stored concurrently as a background task.
    """
    sub = await _nc.subscribe(MEMORY_STORE)
    log.info("Subscribed", extra={"subject": MEMORY_STORE})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            content = data.get("content", "")
            source = data.get("source", "unknown")
            user_id = data.get("user_id", MEMORY_USER_ID)
            metadata = data.get("metadata")

            if not content:
                continue

            asyncio.create_task(_store_memory(content, source, user_id, metadata))

        except Exception:
            log.exception("Error processing memory store request")


async def _ears_listener():
    """Listen for incoming Discord messages via NATS and stream responses to ears."""
    sub = await _nc.subscribe(EARS_MESSAGE_IN)
    log.info("Subscribed", extra={"subject": EARS_MESSAGE_IN})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            channel_id = data.get("channel_id", "")
            message_id = data.get("message_id", "")
            content = data.get("content", "")
            username = data.get("username", "unknown")

            log.info("Discord message", extra={"username": username, "content_len": len(content)})

            # _process_turn streams chunks to EARS_MESSAGE_OUT via forward_to
            await _process_turn(
                content,
                forward_to={"message_id": message_id, "channel_id": channel_id},
            )

        except TimeoutError:
            error_msg = {
                "message_id": data.get("message_id", ""),
                "channel_id": data.get("channel_id", ""),
                "response": "Sorry, I took too long thinking about that. Try again?",
                "done": True,
            }
            await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
        except Exception:
            log.exception("Error processing Discord message")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/turn")
async def turn(req: TurnRequest):
    if not _nc or not _nc.is_connected:
        raise HTTPException(status_code=503, detail="NATS not connected")

    try:
        turn_id, response_text = await _process_turn(req.message)
        return TurnResponse(turn_id=turn_id, response=response_text)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Cortex did not respond in time")
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error during turn processing")


def cli():
    import uvicorn

    uvicorn.run("maki_stem.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
