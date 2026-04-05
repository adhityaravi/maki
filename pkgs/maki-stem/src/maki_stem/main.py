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
from difflib import SequenceMatcher

import httpx
import nats.js.api
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from maki_common import (
    PendingQueues,
    configure_logging,
    connect_nats,
    init_kv,
    kv_put_float,
    parse_config_tags,
    strip_tags,
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
    IMMUNE_STATE_REQUEST,
    MEMORY_STORE,
)
from nats.js.api import RetentionPolicy, StorageType
from pydantic import BaseModel

from maki_stem.loops import CARE_LOOP_SPEC, IDLE_LOOP_SPEC, WORK_LOOP_SPEC, StemContext, _run_loop

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
CONTEXT_TURNS = int(os.environ.get("CONTEXT_TURNS", "15"))
MEMORY_MAX_COUNT = int(os.environ.get("MEMORY_MAX_COUNT", "15"))
MEMORY_MIN_RELEVANCE = float(os.environ.get("MEMORY_MIN_RELEVANCE", "0.5"))
INSTANCE_ID = f"stem-{uuid.uuid4().hex[:8]}"

RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")
MEMORY_USER_ID = os.environ.get("MEMORY_USER_ID", "adi")

CONFIG_BUCKET = "maki-cortex-config"
HEALTH_ENDPOINTS = {
    "recall": RECALL_URL,
    "synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
    "cortex": os.environ.get("CORTEX_URL", "http://maki-cortex:8080"),
}

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
    "max_thoughts_per_day": 1,
    "max_reminders_per_day": 5,
    "work_hours_start": "21:00",
    "work_hours_end": "22:00",
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
_cortex_sessions: dict[str, str] = {}  # instance_id -> session_id
_active_turns: dict[str, float] = {}  # turn_id → start timestamp
_turn_semaphore = asyncio.Semaphore(2)  # limit concurrent cortex turns
_github = None  # GitHubIssueClient, initialized in lifespan if creds available
_sub_response = None  # NATS subscription: CORTEX_TURN_RESPONSE
_sub_cortex_health = None  # NATS subscription: CORTEX_HEALTH
_sub_memory_store = None  # NATS subscription: MEMORY_STORE
_sub_ears = None  # NATS subscription: EARS_MESSAGE_IN


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
    global _sub_response
    _sub_response = await _nc.subscribe(CORTEX_TURN_RESPONSE)
    sub = _sub_response
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

    Tracks sessions per instance_id to support multi-instance cortex.
    """
    global _sub_cortex_health
    _sub_cortex_health = await _nc.subscribe(CORTEX_HEALTH)
    sub = _sub_cortex_health
    log.info("Subscribed", extra={"subject": CORTEX_HEALTH})
    async for msg in sub.messages:
        try:
            payload = json.loads(msg.data.decode())
            session_id = payload.get("session_id")
            if not session_id:
                continue

            instance_id = payload.get("instance_id", session_id)

            if instance_id not in _cortex_sessions:
                _cortex_sessions[instance_id] = session_id
                log.info("Cortex session tracked", extra={"instance_id": instance_id, "session_id": session_id})
                continue

            old_session = _cortex_sessions[instance_id]
            if session_id != old_session:
                _cortex_sessions[instance_id] = session_id
                pending_keys = _pending.pending_keys()
                if pending_keys:
                    cancelled = _pending.cancel_all()
                    log.warning(
                        "Cortex restarted — cancelled stale turns",
                        extra={
                            "instance_id": instance_id,
                            "old_session": old_session,
                            "new_session": session_id,
                            "cancelled_turns": cancelled,
                            "turn_ids": pending_keys,
                        },
                    )
                else:
                    log.info(
                        "Cortex instance restarted (no pending turns)",
                        extra={"instance_id": instance_id, "old_session": old_session, "new_session": session_id},
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


def _build_session_summary() -> str:
    """Build a compact summary of turns that fall outside the recent context window.

    These are turns older than CONTEXT_TURNS that won't appear in _get_recent_conversation().
    Gives cortex awareness of earlier parts of the same session without bloating the full context.
    """
    if len(_conversation_history) <= CONTEXT_TURNS:
        return ""

    older_turns = _conversation_history[:-CONTEXT_TURNS]
    if not older_turns:
        return ""

    lines = [f"Earlier in this session ({len(older_turns)} turns before recent context):"]
    for turn in older_turns:
        user_msg = turn.get("user_message", "")[:120].replace("\n", " ").strip()
        if user_msg:
            lines.append(f"- {user_msg}")

    return "\n".join(lines)


def _deduplicate_memories(memories: list[dict], similarity_threshold: float = 0.82) -> list[dict]:
    """Remove near-duplicate memories, keeping the highest-scoring (first) copy.

    Uses SequenceMatcher ratio on memory text. O(n²) but n is capped at MEMORY_MAX_COUNT
    so this is always fast (~15 comparisons max).
    """
    unique: list[dict] = []
    for candidate in memories:
        text = candidate.get("text", "")
        is_dup = any(
            SequenceMatcher(None, text, existing.get("text", "")).ratio() >= similarity_threshold for existing in unique
        )
        if not is_dup:
            unique.append(candidate)
    return unique


async def _search_memories(query: str) -> tuple[list[dict], list[str]]:
    """Query maki-recall for relevant memories and graph context.

    Fetches up to MEMORY_MAX_COUNT memories with score >= MEMORY_MIN_RELEVANCE.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{RECALL_URL}/search",
                json={"query": query, "user_id": MEMORY_USER_ID, "limit": MEMORY_MAX_COUNT * 2},
            )
            resp.raise_for_status()
            data = resp.json()

        memories = []
        for result in data.get("results", []):
            score = result.get("score", 0)
            if score >= MEMORY_MIN_RELEVANCE:
                memories.append(
                    {
                        "text": result.get("memory", ""),
                        "relevance": round(score, 2),
                    }
                )
        # Cap at max count (already sorted by score descending from recall)
        memories = memories[:MEMORY_MAX_COUNT]
        # Deduplicate near-identical memories (keeps highest-scoring, which come first)
        memories = _deduplicate_memories(memories)

        graph_context = []
        skipped_dangling = 0
        for rel in data.get("relations", []):
            source = rel.get("source") or ""
            relationship = rel.get("relationship") or ""
            target = rel.get("target") or ""
            # Skip dangling or unresolved relationships — any missing endpoint is noise
            if not source or not relationship or not target or source == "?" or target == "?" or relationship == "?":
                skipped_dangling += 1
                continue
            graph_context.append(f"{source} --{relationship}--> {target}")
        if skipped_dangling:
            log.warning("Skipped dangling graph relations", extra={"count": skipped_dangling, "query": query})

        log.info(
            "Memory search complete",
            extra={"memories": len(memories), "relations": len(graph_context), "dangling_skipped": skipped_dangling},
        )
        return memories, graph_context

    except Exception:
        log.exception("Failed to search memories")
        return [], []


async def _feed_memories(user_message: str, cortex_response: str):
    """Feed interaction to maki-recall for autonomous memory extraction."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
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
                log.info("Memory feed complete", extra={"attempt": attempt + 1})
                return
        except httpx.ReadTimeout:
            log.warning("Memory feed timed out", extra={"attempt": attempt + 1})
            if attempt == 0:
                await asyncio.sleep(2.0)
        except Exception:
            log.exception("Failed to feed memories")
            return


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


def _summarize_system_state(system_state: dict) -> str:
    """Return a one-line system health summary for non-health-focused turns."""
    problems = []
    for name, info in system_state.items():
        if not isinstance(info, dict):
            continue
        # Flag unhealthy or restarting components
        healthy = info.get("healthy", True)
        restarts = info.get("restart_count", 0) or info.get("restarts", 0)
        if not healthy:
            problems.append(f"{name}: unhealthy")
        elif restarts and int(restarts) > 3:
            problems.append(f"{name}: {restarts} restarts")
    if problems:
        return "issues: " + ", ".join(problems)
    return "all healthy"


_HEALTH_KEYWORDS = frozenset(
    [
        "health",
        "status",
        "system",
        "component",
        "running",
        "restart",
        "down",
        "broken",
        "error",
        "crash",
        "fail",
        "deploy",
        "pod",
        "service",
        "immune",
        "stem",
        "cortex",
        "recall",
        "synapse",
        "nerve",
        "embed",
    ]
)


def _is_health_query(message: str) -> bool:
    """Return True if the message is asking about system health/status."""
    lower = message.lower()
    return any(kw in lower for kw in _HEALTH_KEYWORDS)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _nc, _js, _config_kv, _lock_kv, _github
    log.info("maki-stem starting", extra={"nats_url": NATS_URL, "instance_id": INSTANCE_ID})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    await _seed_identity()
    await _init_conversation_stream()

    _config_kv = await init_kv(_js, CONFIG_BUCKET)
    _lock_kv = await init_kv(_js, LOCK_BUCKET)

    _github = _init_github_client()

    asyncio.create_task(_response_listener())
    asyncio.create_task(_cortex_heartbeat_watcher())
    asyncio.create_task(_conversation_sync_listener())
    asyncio.create_task(_ears_listener())
    asyncio.create_task(_memory_store_listener())
    ctx = StemContext(
        nc=_nc,
        js=_js,
        kv=_kv,
        lock_kv=_lock_kv,
        config_kv=_config_kv,
        pending=_pending,
        github=_github,
        instance_id=INSTANCE_ID,
        default_config=DEFAULT_CORTEX_CONFIG,
        search_memories=_search_memories,
        feed_memories=_feed_memories,
        gather_system_state=_gather_system_state,
        format_system_state=_format_system_state,
        get_recent_conversation=_get_recent_conversation,
        in_quiet_hours=_in_quiet_hours,
        in_work_hours=_in_work_hours,
    )
    asyncio.create_task(_run_loop(IDLE_LOOP_SPEC, ctx))
    asyncio.create_task(_run_loop(CARE_LOOP_SPEC, ctx))
    asyncio.create_task(_run_loop(WORK_LOOP_SPEC, ctx))

    yield

    await _nc.close()


app = FastAPI(title="maki-stem", version="0.0.1", lifespan=lifespan)


class TurnRequest(BaseModel):
    message: str


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

    # Include full system state only for health-related queries; otherwise send a summary
    if _is_health_query(message):
        turn_system_state = system_state
        turn_system_state_summary = None
    else:
        turn_system_state = None
        turn_system_state_summary = _summarize_system_state(system_state)

    turn_payload = {
        "turn_id": turn_id,
        "identity": identity,
        "conversation": _get_recent_conversation(),
        "session_summary": _build_session_summary(),
        "memories": memories,
        "graph_context": graph_context,
        "system_state": turn_system_state,
        "system_state_summary": turn_system_state_summary,
        "prompt": message,
        "mission_results": None,
    }

    queue = _pending.create(turn_id)
    full_response = []

    try:
        await _nc.publish(CORTEX_TURN_REQUEST, json.dumps(turn_payload).encode())
        log.info("Turn request published", extra={"turn_id": turn_id})

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT)
            except TimeoutError:
                log.error("Turn timed out", extra={"turn_id": turn_id})
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
                raise

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
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(ears_msg).encode())

            if done:
                break

        cortex_response = "".join(full_response)
        clean_response = strip_tags(cortex_response)
        config_updates = parse_config_tags(cortex_response)
        if config_updates:
            await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CORTEX_CONFIG.keys()))

        asyncio.create_task(_publish_turn_to_stream(turn_id, message, clean_response))
        asyncio.create_task(_feed_memories(message, clean_response))

        return turn_id, clean_response

    finally:
        _pending.remove(turn_id)
        _active_turns.pop(turn_id, None)


async def _store_memory(content: str, source: str, user_id: str, metadata: dict | None):
    """Store a single memory via recall REST API (runs as background task)."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{RECALL_URL}/memories",
                    json={
                        "messages": [{"role": "user", "content": content}],
                        "user_id": user_id,
                        "metadata": metadata or {},
                    },
                )
                resp.raise_for_status()
                log.info("Memory stored", extra={"source": source, "attempt": attempt + 1})
                return
        except httpx.ReadTimeout:
            log.warning("Memory store timed out", extra={"source": source, "attempt": attempt + 1})
            if attempt == 0:
                await asyncio.sleep(2.0)
        except Exception:
            log.exception("Failed to store memory", extra={"source": source})
            return


async def _memory_store_listener():
    """Listen for memory store requests from any component via NATS.

    Any component can publish to MEMORY_STORE with:
    {"content": "...", "user_id": "...", "metadata": {...}}
    Each memory is stored concurrently as a background task.
    """
    global _sub_memory_store
    _sub_memory_store = await _nc.subscribe(MEMORY_STORE, queue="maki-stem")
    sub = _sub_memory_store
    log.info("Subscribed", extra={"subject": MEMORY_STORE})
    async for msg in sub.messages:
        try:
            data = json.loads(msg.data.decode())
            content = data.get("content", "").strip()
            if not content:
                continue

            user_id = data.get("user_id", MEMORY_USER_ID)
            source = data.get("source", "unknown")
            metadata = data.get("metadata")

            asyncio.create_task(_store_memory(content, source, user_id, metadata))

        except Exception:
            log.exception("Error in memory store listener")


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
            try:
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
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
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
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
                await _nc.publish(EARS_MESSAGE_OUT, json.dumps(error_msg).encode())
            except Exception:
                log.exception("Failed to send error to ears")


async def _ears_listener():
    """Listen for incoming Discord messages via NATS and dispatch as tasks."""
    global _sub_ears
    _sub_ears = await _nc.subscribe(EARS_MESSAGE_IN, queue="maki-stem")
    sub = _sub_ears
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
    if not _nc or not _nc.is_connected:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "reason": "NATS not connected"})

    dead_subs = []
    for name, sub in [
        ("response", _sub_response),
        ("cortex_health", _sub_cortex_health),
        ("memory_store", _sub_memory_store),
        ("ears", _sub_ears),
    ]:
        if sub is None or getattr(sub, "_closed", False):
            dead_subs.append(name)
    if dead_subs:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "NATS subscriptions closed", "dead": dead_subs},
        )

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
    _, response = await _process_turn(req.message)
    return {"response": response}


def cli():
    import uvicorn

    uvicorn.run("maki_stem.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
