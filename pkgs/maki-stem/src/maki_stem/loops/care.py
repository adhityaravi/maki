"""Care check-in loop — periodic gentle reminders when the user is inactive."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime

from maki_common import kv_get_float, strip_tags
from maki_common.subjects import CORTEX_TURN_REQUEST, EARS_REMINDER_OUT

from .base import RECENTLY_ACTIVE_THRESHOLD, TOOLS_PROMPT, LoopSpec, StemContext, cron_window

log = logging.getLogger(__name__)

CARE_CHECK_INTERVAL = int(os.environ.get("CARE_CHECK_INTERVAL", "60"))
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "1800"))

# Care fires once daily at 08:00
CARE_CRON = "0 8 * * *"

MAX_RECENT_REMINDERS = 5  # how many past reminders to inject as dedup context
CARE_REMINDERS_KEY = "care.recent_reminders"  # NATS KV key for reminder history

KV_KEY = "identity"
_DEFAULT_IDENTITY_FALLBACK = "You are Maki."

_CARE_PROMPT = """## Care Mode

You are checking in on Adi. This is not a conversation — it's you paying attention.

You have memories of recent interactions. Look for:
- Things Adi said he'd do ("I'll deploy that tomorrow", "need to check the pricing")
- Projects that seem stuck or abandoned
- Deadlines or time-sensitive things mentioned
- Patterns worth pointing out ("you've been working on X for two weeks, the Y part keeps blocking you")
- Things that would be helpful to surface right now given the time/day

## Relevant memories
{memories}

## Graph context
{graph_context}

## Time
Local time: {local_time}, {day_of_week}
Last interaction: {hours_since}h ago

## Rules
- Write a short, natural reminder or nudge. Like a friend who pays attention, not a calendar app.
- One thing per message. Don't dump a list.
- If there's genuinely nothing worth saying right now → respond with exactly [SILENT]
- Don't be annoying. If you reminded about something recently, don't repeat it.
- You can be proactive — "hey, you mentioned wanting to test the HA setup, \
the system's been stable for 6 hours, good window for it" is great.
- Store any new patterns you notice with add_memory."""


def _build_care_system_prompt(
    identity: str,
    memories: list,
    graph_context: list,
    care_context: dict,
) -> str:
    """Assemble the complete system prompt for a care check-in turn."""
    time_ctx = care_context.get("time_context", {})

    mem_lines = [f"- {m['text']} (relevance: {m.get('relevance', '?')})" for m in memories]
    mem_str = "\n".join(mem_lines) if mem_lines else "No recent memories found."

    graph_lines = [f"- {r}" for r in graph_context]
    graph_str = "\n".join(graph_lines) if graph_lines else "No graph context."

    parts = []
    if identity:
        parts.append(identity)

    parts.append(
        _CARE_PROMPT.format(
            memories=mem_str,
            graph_context=graph_str,
            hours_since=care_context.get("hours_since_last_interaction", "?"),
            local_time=time_ctx.get("local_time", "?"),
            day_of_week=datetime.now().strftime("%A"),
        )
    )

    parts.append(TOOLS_PROMPT)
    return "\n\n".join(parts)


# Module-level daily counters
_reminders_today: int = 0
_reminders_today_date: str = ""


async def _get_recent_reminders(ctx: StemContext) -> list[str]:
    """Load the last MAX_RECENT_REMINDERS reminder texts from NATS KV."""
    try:
        entry = await ctx.lock_kv.get(CARE_REMINDERS_KEY)
        return json.loads(entry.value.decode())
    except Exception:
        return []


async def _put_recent_reminder(text: str, ctx: StemContext) -> None:
    """Append a reminder text to the KV history, capped at MAX_RECENT_REMINDERS."""
    try:
        recent = await _get_recent_reminders(ctx)
        recent.append(text[:300])
        recent = recent[-MAX_RECENT_REMINDERS:]
        await ctx.lock_kv.put(CARE_REMINDERS_KEY, json.dumps(recent).encode())
    except Exception:
        log.warning("Failed to persist recent reminder to KV", exc_info=True)


async def _care_should_run(config: dict, ctx: StemContext) -> bool:
    """Guard checks for the care loop: cron window, activity threshold, daily cap.

    Fires at 08:00 every day — one reminder per day max.
    """
    global _reminders_today, _reminders_today_date

    if not cron_window(CARE_CRON):
        return False

    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    if time.time() - last_activity < RECENTLY_ACTIVE_THRESHOLD:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    if today != _reminders_today_date:
        _reminders_today = 0
        _reminders_today_date = today

    max_reminders = config.get("max_reminders_per_day", 1)
    return _reminders_today < max_reminders


async def _care_body(spec: LoopSpec, config: dict, ctx: StemContext) -> None:
    """Execute one care check-in cycle."""
    global _reminders_today

    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    max_reminders = config.get("max_reminders_per_day", 5)

    try:
        entry = await ctx.kv.get(KV_KEY)
        identity = entry.value.decode()
    except Exception:
        identity = _DEFAULT_IDENTITY_FALLBACK

    memories, graph_context = await ctx.search_memories("Adi's wellbeing, habits, recent concerns")
    recent_reminders = await _get_recent_reminders(ctx)

    turn_id = f"care-{uuid.uuid4().hex[:8]}"
    care_context = {
        "hours_since_last_interaction": round((time.time() - last_activity) / 3600, 1),
        "time_context": {
            "local_time": datetime.now().strftime("%H:%M"),
        },
        "recent_reminders": recent_reminders,
    }
    care_payload = {
        "turn_id": turn_id,
        "mode": "care",
        "identity": identity,
        "conversation": ctx.get_recent_conversation(),
        "memories": memories,
        "graph_context": graph_context,
        "prompt": None,
        "mission_results": None,
        "care_context": care_context,
        "system_prompt": _build_care_system_prompt(identity, memories, graph_context, care_context),
    }

    queue = ctx.pending.create(turn_id)
    try:
        await ctx.nc.publish(CORTEX_TURN_REQUEST, json.dumps(care_payload).encode())
        log.info("Care turn published", extra={"turn_id": turn_id})

        response_data = await asyncio.wait_for(queue.get(), timeout=TURN_TIMEOUT)
        reminder = response_data.get("response", "")

        clean_reminder = strip_tags(reminder or "")
        if clean_reminder:
            reminder_payload = {"reminder": clean_reminder, "turn_id": turn_id}
            await ctx.nc.publish(EARS_REMINDER_OUT, json.dumps(reminder_payload).encode())
            asyncio.create_task(_put_recent_reminder(clean_reminder, ctx))
            _reminders_today += 1
            log.info(
                "Reminder published",
                extra={
                    "turn_id": turn_id,
                    "reminders_today": _reminders_today,
                    "max": max_reminders,
                },
            )

            asyncio.create_task(
                ctx.feed_memories(
                    "[Care check-in]",
                    clean_reminder,
                )
            )

    except TimeoutError:
        log.error("Care turn timed out", extra={"turn_id": turn_id})
    except Exception:
        log.exception("Care turn failed", extra={"turn_id": turn_id})
    finally:
        ctx.pending.remove(turn_id)


CARE_LOOP_SPEC = LoopSpec(
    name="care",
    check_interval_getter=lambda: CARE_CHECK_INTERVAL,
    execution_interval_getter=lambda config: 86400,  # once per day — lock TTL prevents double-fire
    should_run=_care_should_run,
    body=_care_body,
)
