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
import nats.js.api
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from maki_common import (
    PendingQueues,
    configure_logging,
    connect_nats,
    init_kv,
    kv_get_float,
    kv_put_float,
    load_kv_config,
    parse_config_tags,
    strip_tags,
    try_claim_loop,
)
from maki_common.config import apply_config_updates
from maki_common.subjects import (
    CONVERSATION_STREAM,
    CORTEX_HEALTH,
    CORTEX_STUCK,
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
LOCK_BUCKET = "maki-lock"

STREAM_NAME = "maki-conversation"
STREAM_MAX_MSGS = int(os.environ.get("STREAM_MAX_MSGS", "200"))
CONTEXT_TURNS = int(os.environ.get("CONTEXT_TURNS", "20"))
INSTANCE_ID = f"stem-{uuid.uuid4().hex[:8]}"

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

WORK_CHECK_INTERVAL = int(os.environ.get("WORK_CHECK_INTERVAL", "300"))
WORK_TURN_TIMEOUT = int(os.environ.get("WORK_TURN_TIMEOUT", "2700"))  # 45 minutes
USER_INACTIVE_THRESHOLD = 7200  # 2 hours

# GitHub App config (optional — enables issue tracking for idle/work loops)
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY_PATH = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
GITHUB_INSTALLATION_ID = os.environ.get("GITHUB_INSTALLATION_ID")
REPO_OWNER = os.environ.get("REPO_OWNER", "adhityaravi")
REPO_NAME = os.environ.get("REPO_NAME", "maki")

DEFAULT_CORTEX_CONFIG = {
    "idle_interval": 7200,
    "care_interval": 1800,
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "07:00",
    "max_thoughts_per_day": 5,
    "max_reminders_per_day": 5,
    "work_hours_start": "01:00",
    "work_hours_end": "06:00",
    "max_work_items_per_night": 2,
    "work_cooldown_minutes": 15,
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
_lock_kv = None
_pending = PendingQueues()
_conversation_history: list[dict] = []
_thoughts_today: int = 0
_thoughts_today_date: str = ""
_reminders_today: int = 0
_reminders_today_date: str = ""
_work_items_tonight: int = 0
_work_items_tonight_date: str = ""
_cortex_session_id: str | None = None
_active_turns: dict[str, float] = {}  # turn_id → start timestamp
_turn_semaphore = asyncio.Semaphore(2)  # limit concurrent cortex turns
_github = None  # GitHubIssueClient, initialized in lifespan if creds available


def _init_github_client():
    """Initialize GitHub issue client if credentials are available."""
    if not (GITHUB_APP_ID and GITHUB_PRIVATE_KEY_PATH and GITHUB_INSTALLATION_ID):
        log.info("GitHub credentials not configured — issue tracking disabled")
        return None

    try:
        with open(GITHUB_PRIVATE_KEY_PATH) as f:
            private_key = f.read()
    except Exception:
        log.exception("Failed to read GitHub private key")
        return None

    from maki_common.github_client import GitHubIssueClient

    client = GitHubIssueClient(
        app_id=GITHUB_APP_ID,
        private_key=private_key,
        installation_id=GITHUB_INSTALLATION_ID,
        default_owner=REPO_OWNER,
        default_repo=REPO_NAME,
    )
    log.info("GitHub issue client initialized")
    return client


def _truncate_for_title(text: str, max_len: int = 80) -> str:
    """Truncate text to make a reasonable issue title."""
    # Take first line or first sentence
    first_line = text.split("\n")[0].strip()
    if len(first_line) > max_len:
        return first_line[: max_len - 3] + "..."
    return first_line


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


async def _cortex_heartbeat_watcher():
    """Watch cortex heartbeat for session_id changes (restarts).

    When cortex restarts mid-turn, its session_id changes. We detect this
    and cancel all pending turns immediately instead of waiting 30 minutes
    for the timeout.
    """
    global _cortex_session_id
    sub = await _nc.subscribe(CORTEX_HEALTH)
    log.info("Subscribed", extra={"subject": CORTEX_HEALTH})
    async for msg in sub.messages:
        try:
            payload = json.loads(msg.data.decode())
            session_id = payload.get("session_id")
            if not session_id:
                continue

            if _cortex_session_id is None:
                _cortex_session_id = session_id
                log.info("Cortex session tracked", extra={"session_id": session_id})
                continue

            if session_id != _cortex_session_id:
                old_session = _cortex_session_id
                _cortex_session_id = session_id
                pending_keys = _pending.pending_keys()
                if pending_keys:
                    cancelled = _pending.cancel_all()
                    log.warning(
                        "Cortex restarted — cancelled stale turns",
                        extra={
                            "old_session": old_session,
                            "new_session": session_id,
                            "cancelled_turns": cancelled,
                            "turn_ids": pending_keys,
                        },
                    )
                else:
                    log.info(
                        "Cortex session changed (no pending turns)",
                        extra={"old_session": old_session, "new_session": session_id},
                    )
        except Exception:
            log.exception("Error in cortex heartbeat watcher")


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
        await _js.find_stream_name_by_subject(CONVERSATION_STREAM)
        log.info("Conversation stream exists", extra={"stream": STREAM_NAME})
    except Exception:
        await _js.add_stream(
            name=STREAM_NAME,
            subjects=[CONVERSATION_STREAM],
            retention=RetentionPolicy.LIMITS,
            max_msgs=STREAM_MAX_MSGS,
            storage=StorageType.FILE,
        )
        log.info("Created conversation stream", extra={"stream": STREAM_NAME, "max_msgs": STREAM_MAX_MSGS})

    try:
        sub = await _js.subscribe(CONVERSATION_STREAM, ordered_consumer=True)
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


async def _conversation_sync_listener():
    """Live subscriber to conversation stream — keeps _conversation_history in sync.

    Ensures that all stem instances see turns processed by any instance.
    Uses a durable push consumer so we don't miss messages while running.
    """
    # Use deliver_last_per_subject to start from where we left off (after startup replay)
    # Subscribe with a unique durable name per instance to get independent delivery
    consumer_name = f"stem-sync-{INSTANCE_ID}"
    try:
        sub = await _js.subscribe(
            CONVERSATION_STREAM,
            durable=consumer_name,
            deliver_policy=nats.js.api.DeliverPolicy.LAST_PER_SUBJECT,
        )
    except Exception:
        # Fallback: ordered consumer starting from now (may miss some, but won't crash)
        sub = await _js.subscribe(CONVERSATION_STREAM, ordered_consumer=True)

    log.info("Conversation sync listener started", extra={"instance_id": INSTANCE_ID})
    async for msg in sub.messages:
        try:
            turn_doc = json.loads(msg.data.decode())
            turn_id = turn_doc.get("turn_id", "")

            # Skip if we already have this turn (we added it locally in _publish_turn_to_stream)
            if any(t.get("turn_id") == turn_id for t in _conversation_history[-50:]):
                await msg.ack()
                continue

            _conversation_history.append(turn_doc)

            # Keep bounded
            while len(_conversation_history) > STREAM_MAX_MSGS:
                _conversation_history.pop(0)

            log.info(
                "Conversation synced from stream",
                extra={"turn_id": turn_id, "instance": turn_doc.get("instance_id", "?")},
            )
            await msg.ack()
        except Exception:
            log.exception("Error syncing conversation")


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
        ack = await _js.publish(CONVERSATION_STREAM, json.dumps(turn_doc).encode())
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


def _in_work_hours(config: dict) -> bool:
    """Check if current time is within work hours."""
    now = datetime.now()
    current = now.hour * 60 + now.minute

    start_parts = config.get("work_hours_start", "01:00").split(":")
    end_parts = config.get("work_hours_end", "06:00").split(":")
    start = int(start_parts[0]) * 60 + int(start_parts[1])
    end = int(end_parts[0]) * 60 + int(end_parts[1])

    if start > end:  # spans midnight
        return current >= start or current < end
    return start <= current < end


async def _idle_loop():
    """Proactive idle heartbeat loop — Maki's inner life."""
    global _thoughts_today, _thoughts_today_date

    log.info("Idle loop started", extra={"check_interval": IDLE_CHECK_INTERVAL, "instance_id": INSTANCE_ID})

    while True:
        await asyncio.sleep(IDLE_CHECK_INTERVAL)

        try:
            config = await load_kv_config(_config_kv, DEFAULT_CORTEX_CONFIG)
            idle_interval = config.get("idle_interval", 7200)

            if not await try_claim_loop(_lock_kv, "loop.stem.idle", idle_interval, INSTANCE_ID):
                continue

            last_activity = await kv_get_float(_lock_kv, "stem.last_activity", default=time.time())
            if time.time() - last_activity < RECENTLY_ACTIVE_THRESHOLD:
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
                    "last_interaction": datetime.fromtimestamp(last_activity, tz=UTC).isoformat(),
                    "hours_since_last_interaction": round((time.time() - last_activity) / 3600, 1),
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

                    # Create GitHub issue for the thought
                    if _github:
                        asyncio.create_task(_create_idle_thought_issue(clean_thought, turn_id))
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


async def _create_idle_thought_issue(thought: str, turn_id: str):
    """Create a GitHub issue for an idle loop thought."""
    try:
        title = f"[Thought] {_truncate_for_title(thought, max_len=70)}"
        body = (
            f"{thought}\n\n"
            f"---\n"
            f"*Generated by maki idle loop*\n"
            f"Turn: `{turn_id}`\n"
            f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        issue_num = await _github.create_issue(
            title=title,
            body=body,
            labels=["maki-thought", "automated"],
        )
        if issue_num:
            log.info(
                "Idle thought issue created",
                extra={"turn_id": turn_id, "issue": issue_num},
            )
    except Exception:
        log.exception("Failed to create idle thought issue", extra={"turn_id": turn_id})


async def _care_loop():
    """Proactive care loop — Maki checking in on Adi."""
    global _reminders_today, _reminders_today_date

    log.info("Care loop started", extra={"check_interval": CARE_CHECK_INTERVAL, "instance_id": INSTANCE_ID})

    while True:
        await asyncio.sleep(CARE_CHECK_INTERVAL)

        try:
            config = await load_kv_config(_config_kv, DEFAULT_CORTEX_CONFIG)
            care_interval = config.get("care_interval", 1800)

            if not await try_claim_loop(_lock_kv, "loop.stem.care", care_interval, INSTANCE_ID):
                continue

            last_activity = await kv_get_float(_lock_kv, "stem.last_activity", default=time.time())
            if time.time() - last_activity < RECENTLY_ACTIVE_THRESHOLD:
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
                    "hours_since_last_interaction": round((time.time() - last_activity) / 3600, 1),
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


async def _work_loop():
    """Night work loop — execute queued todos while the user sleeps."""
    global _work_items_tonight, _work_items_tonight_date

    log.info("Work loop started", extra={"check_interval": WORK_CHECK_INTERVAL})

    while True:
        await asyncio.sleep(WORK_CHECK_INTERVAL)

        try:
            config = await load_kv_config(_config_kv, DEFAULT_CORTEX_CONFIG)

            if not _in_work_hours(config):
                continue

            work_interval = config.get("work_interval", WORK_CHECK_INTERVAL)
            if not await try_claim_loop(_lock_kv, "loop.stem.work", work_interval, INSTANCE_ID):
                continue

            # Reset nightly counter if date changed
            tonight = datetime.now().strftime("%Y-%m-%d")
            if tonight != _work_items_tonight_date:
                _work_items_tonight = 0
                _work_items_tonight_date = tonight

            max_items = config.get("max_work_items_per_night", 2)
            if _work_items_tonight >= max_items:
                continue

            # Only work if user has been inactive
            last_activity = await kv_get_float(_lock_kv, "stem.last_activity", default=time.time())
            if time.time() - last_activity < USER_INACTIVE_THRESHOLD:
                log.info("Work loop: user recently active, skipping")
                continue

            # Pull work items from GitHub issues (single source of truth)
            if not _github:
                log.info("Work loop: GitHub client not available, skipping")
                continue

            issues = await _github.list_issues(state="open")
            if not issues:
                continue

            issue = issues[0]  # Highest priority (list_issues sorts by P-label)
            issue_number = issue["number"]
            issue_title = issue["title"]
            issue_body = issue.get("body", "") or ""

            # Extract priority from labels (default P3)
            issue_priority = 3
            for label in issue.get("labels", []):
                label_name = label.get("name", "") if isinstance(label, dict) else str(label)
                if label_name in ("P1", "P2", "P3", "P4", "P5"):
                    issue_priority = int(label_name[1])
                    break

            log.info(
                "Work loop: starting task",
                extra={"issue": issue_number, "title": issue_title, "priority": issue_priority},
            )

            # Comment on issue that work is starting
            asyncio.create_task(
                _github.comment_issue(
                    issue_number,
                    f"🔧 **Starting work on this task.**\n\nTime: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
                )
            )

            # Load identity
            try:
                entry = await _kv.get(KV_KEY)
                identity = entry.value.decode()
            except Exception:
                identity = DEFAULT_IDENTITY

            turn_id = f"work-{uuid.uuid4().hex[:8]}"
            work_payload = {
                "turn_id": turn_id,
                "mode": "work",
                "identity": identity,
                "conversation": [],
                "memories": [],
                "graph_context": [],
                "prompt": None,
                "work_context": {
                    "issue_number": issue_number,
                    "issue_title": issue_title,
                    "issue_description": issue_body,
                    "issue_priority": issue_priority,
                },
            }

            queue = _pending.create(turn_id)

            try:
                await _nc.publish(CORTEX_TURN_REQUEST, json.dumps(work_payload).encode())
                log.info("Work turn published", extra={"turn_id": turn_id, "issue": issue_number})

                response_data = await asyncio.wait_for(queue.get(), timeout=WORK_TURN_TIMEOUT)
                result_text = response_data.get("response", "")
                _work_items_tonight += 1

                clean_result = strip_tags(result_text or "")
                if clean_result and clean_result != "[SILENT]":
                    thought_payload = {
                        "thought": f"[Night work] Completed: {issue_title}\n{clean_result}",
                        "turn_id": turn_id,
                    }
                    await _nc.publish(EARS_THOUGHT_OUT, json.dumps(thought_payload).encode())

                log.info(
                    "Work turn complete",
                    extra={
                        "turn_id": turn_id,
                        "issue": issue_number,
                        "work_items_tonight": _work_items_tonight,
                        "max": max_items,
                    },
                )

                asyncio.create_task(
                    _feed_memories(
                        f"[Night work] Task: {issue_title} (priority P{issue_priority})",
                        clean_result or "Task completed",
                    )
                )

                # Close the GitHub issue with result
                close_comment = (
                    f"✅ **Task completed.**\n\n"
                    f"{clean_result or 'No detailed result.'}\n\n"
                    f"---\n"
                    f"Turn: `{turn_id}`\n"
                    f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
                )
                asyncio.create_task(_github.close_issue(issue_number, comment=close_comment))

            except TimeoutError:
                log.error("Work turn timed out", extra={"turn_id": turn_id, "issue": issue_number})
                # Publish stuck signal for immune
                await _nc.publish(
                    CORTEX_STUCK,
                    json.dumps(
                        {
                            "turn_id": turn_id,
                            "mode": "work",
                            "timeout_seconds": WORK_TURN_TIMEOUT,
                            "user_waiting": False,
                        }
                    ).encode(),
                )

                # Comment on issue about timeout (issue stays open for retry)
                asyncio.create_task(
                    _github.comment_issue(
                        issue_number,
                        f"⏱️ **Work timed out** after {WORK_TURN_TIMEOUT}s. Will retry next work session.",
                    )
                )

            except Exception:
                log.exception("Work turn failed", extra={"turn_id": turn_id, "issue": issue_number})

                # Comment on issue about failure (issue stays open for retry)
                asyncio.create_task(
                    _github.comment_issue(
                        issue_number,
                        "❌ **Work failed** due to an error. Will retry next work session.",
                    )
                )

            finally:
                _pending.remove(turn_id)

            # Cooldown between work items
            cooldown = config.get("work_cooldown_minutes", 15) * 60
            log.info("Work cooldown", extra={"cooldown_seconds": cooldown})
            await asyncio.sleep(cooldown)

        except Exception:
            log.exception("Work loop error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _nc, _js, _config_kv, _lock_kv, _github
    log.info("maki-stem starting", extra={"nats_url": NATS_URL, "instance_id": INSTANCE_ID})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    await _seed_identity()
    await _init_conversation_stream()
    _config_kv = await init_kv(_js, CONFIG_BUCKET, defaults=DEFAULT_CORTEX_CONFIG)
    _lock_kv = await init_kv(_js, LOCK_BUCKET)

    _github = _init_github_client()

    asyncio.create_task(_response_listener())
    asyncio.create_task(_cortex_heartbeat_watcher())
    asyncio.create_task(_conversation_sync_listener())
    asyncio.create_task(_ears_listener())
    asyncio.create_task(_memory_store_listener())
    asyncio.create_task(_idle_loop())
    asyncio.create_task(_care_loop())
    asyncio.create_task(_work_loop())

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
    await kv_put_float(_lock_kv, "stem.last_activity", time.time())

    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    _active_turns[turn_id] = time.time()
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
            cancelled = data.get("cancelled", False)

            if cancelled:
                log.warning("Turn cancelled (cortex restarted)", extra={"turn_id": turn_id})
                raise RuntimeError("Turn cancelled: cortex restarted mid-turn")

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

        # Parse and apply any config tags from cortex response
        config_updates = parse_config_tags(response_text)
        await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CORTEX_CONFIG.keys()))

        log.info("Turn complete", extra={"turn_id": turn_id, "chunks": len(chunks)})

        await _publish_turn_to_stream(
            turn_id=turn_id,
            user_message=message,
            cortex_response=response_text,
        )

        asyncio.create_task(_feed_memories(message, response_text))

        return turn_id, response_text

    except TimeoutError:
        # Publish stuck signal for immune awareness
        try:
            await _nc.publish(
                CORTEX_STUCK,
                json.dumps(
                    {
                        "turn_id": turn_id,
                        "mode": "normal",
                        "timeout_seconds": TURN_TIMEOUT,
                        "user_waiting": True,
                    }
                ).encode(),
            )
        except Exception:
            log.exception("Failed to publish stuck signal")
        raise
    except Exception:
        log.exception("Turn failed", extra={"turn_id": turn_id})
        raise
    finally:
        _pending.remove(turn_id)
        _active_turns.pop(turn_id, None)


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
    sub = await _nc.subscribe(MEMORY_STORE, queue="maki-stem")
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


async def _handle_discord_message(data: dict):
    """Handle a single Discord message (runs as independent task)."""
    channel_id = data.get("channel_id", "")
    message_id = data.get("message_id", "")
    content = data.get("content", "")
    forward_to = {"message_id": message_id, "channel_id": channel_id}

    async with _turn_semaphore:
        try:
            await _process_turn(content, forward_to=forward_to)
        except TimeoutError:
            error_msg = {
                "message_id": message_id,
                "channel_id": channel_id,
                "response": "Sorry, I took too long thinking about that. Try again?",
                "done": True,
            }
            await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
        except RuntimeError as e:
            log.warning("Turn cancelled", extra={"error": str(e)})
            try:
                error_msg = {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "response": "I lost my train of thought (my brain restarted). What were you saying?",
                    "done": True,
                }
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
            except Exception:
                log.exception("Failed to send cancelled signal to ears")
        except Exception:
            log.exception("Error processing Discord message")
            try:
                error_msg = {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "response": "",
                    "done": True,
                }
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
            except Exception:
                log.exception("Failed to send done signal to ears")


async def _ears_listener():
    """Listen for incoming Discord messages via NATS and dispatch as tasks."""
    sub = await _nc.subscribe(EARS_MESSAGE_IN, queue="maki-stem")
    log.info("Subscribed", extra={"subject": EARS_MESSAGE_IN})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            username = data.get("username", "unknown")
            log.info("Discord message", extra={"username": username, "content_len": len(data.get("content", ""))})
            asyncio.create_task(_handle_discord_message(data))
        except Exception:
            log.exception("Error dispatching Discord message")


@app.get("/health")
def health():
    now = time.time()
    for turn_id, started in _active_turns.items():
        if now - started > TURN_TIMEOUT:
            return JSONResponse(
                status_code=503,
                content={"status": "stuck", "turn_id": turn_id, "running_seconds": int(now - started)},
            )
    return {"status": "ok", "active_turns": len(_active_turns)}


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
