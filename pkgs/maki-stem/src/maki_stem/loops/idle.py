"""Idle reflection loop — periodic unprompted thoughts when the user is inactive."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

from maki_common import kv_get_float, parse_config_tags, strip_tags
from maki_common.config import apply_config_updates
from maki_common.subjects import CORTEX_TURN_REQUEST, EARS_THOUGHT_OUT

from .base import RECENTLY_ACTIVE_THRESHOLD, LoopSpec, StemContext

log = logging.getLogger(__name__)

IDLE_CHECK_INTERVAL = int(os.environ.get("IDLE_CHECK_INTERVAL", "60"))
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "1800"))

# Day-of-week scheduling: idle runs Mon/Wed/Fri/Sun, work runs Tue/Thu/Sat.
# Python weekday(): 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
_IDLE_DAYS: frozenset[int] = frozenset({0, 2, 4, 6})  # Mon, Wed, Fri, Sun
_PROACTIVE_WINDOW_HOUR = 21  # 9 PM local — loop fires once in the 21:00–22:00 window

KV_KEY = "identity"
_DEFAULT_IDENTITY_FALLBACK = "You are Maki."

# Rotating memory search queries — varied by hour so each cycle surfaces different memories
_IDLE_MEMORY_QUERIES = [
    "recent work and open problems",
    "infrastructure and system health patterns",
    "Adi's projects and goals",
    "code quality issues and technical debt",
    "decisions made and their outcomes",
    "recurring patterns and blockers",
    "things Adi mentioned but never followed up on",
    "creative ideas and future plans",
]

# Module-level daily counters
_thoughts_today: int = 0
_thoughts_today_date: str = ""


async def _idle_should_run(config: dict, ctx: StemContext) -> bool:
    """Guard checks for the idle loop: scheduled window, activity threshold, daily cap.

    Idle loop fires on Mon/Wed/Fri/Sun between 21:00–22:00 only — one thought per day.
    """
    global _thoughts_today, _thoughts_today_date

    now = datetime.now()
    if now.weekday() not in _IDLE_DAYS or now.hour != _PROACTIVE_WINDOW_HOUR:
        return False

    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    if time.time() - last_activity < RECENTLY_ACTIVE_THRESHOLD:
        return False

    today = now.strftime("%Y-%m-%d")
    if today != _thoughts_today_date:
        _thoughts_today = 0
        _thoughts_today_date = today

    max_thoughts = config.get("max_thoughts_per_day", 1)
    return _thoughts_today < max_thoughts


async def _idle_body(spec: LoopSpec, config: dict, ctx: StemContext) -> None:
    """Execute one idle reflection cycle."""
    global _thoughts_today

    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    max_thoughts = config.get("max_thoughts_per_day", 5)

    try:
        entry = await ctx.kv.get(KV_KEY)
        identity = entry.value.decode()
    except Exception:
        identity = _DEFAULT_IDENTITY_FALLBACK

    idle_query = _IDLE_MEMORY_QUERIES[int(time.time() / 3600) % len(_IDLE_MEMORY_QUERIES)]
    memories, graph_context = await ctx.search_memories(idle_query)
    system_state = await ctx.gather_system_state()

    # Fetch open issues for dedup — injected into cortex prompt so it doesn't
    # need to call list_issues itself and can suppress duplicates reliably.
    open_issues: list[dict] = []
    if ctx.github:
        try:
            issues = await ctx.github.list_issues(state="open")
            open_issues = [{"number": i.get("number"), "title": i.get("title", "")} for i in (issues or [])]
        except Exception:
            log.warning("Failed to fetch open issues for idle dedup")

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
            },
            "current_config": config,
            "system_state": system_state,
            "open_issues": open_issues,
        },
    }

    queue = ctx.pending.create(turn_id)
    try:
        await ctx.nc.publish(CORTEX_TURN_REQUEST, json.dumps(idle_payload).encode())
        log.info("Idle turn published", extra={"turn_id": turn_id})

        # Idle reflection is single-shot — one response with done=True
        response_data = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT)
        thought = response_data.get("response", "")

        clean_thought = strip_tags(thought or "")
        config_updates = parse_config_tags(thought or "")
        if config_updates:
            await apply_config_updates(ctx.config_kv, config_updates, allowed_keys=set(ctx.default_config.keys()))

        if clean_thought:
            thought_payload = {"thought": clean_thought, "turn_id": turn_id}
            await ctx.nc.publish(EARS_THOUGHT_OUT, json.dumps(thought_payload).encode())
            _thoughts_today += 1
            log.info(
                "Thought published",
                extra={
                    "turn_id": turn_id,
                    "thoughts_today": _thoughts_today,
                    "max": max_thoughts,
                },
            )

            state_summary = ctx.format_system_state(system_state)
            asyncio.create_task(
                ctx.feed_memories(
                    f"[Idle reflection] System state: {state_summary}",
                    clean_thought,
                )
            )

    except TimeoutError:
        log.error("Idle turn timed out", extra={"turn_id": turn_id})
    except Exception:
        log.exception("Idle turn failed", extra={"turn_id": turn_id})
    finally:
        ctx.pending.remove(turn_id)


IDLE_LOOP_SPEC = LoopSpec(
    name="idle",
    check_interval_getter=lambda: IDLE_CHECK_INTERVAL,
    execution_interval_getter=lambda config: config.get("idle_interval", 7200),
    should_run=_idle_should_run,
    body=_idle_body,
)
