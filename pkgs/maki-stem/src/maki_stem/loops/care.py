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

from .base import RECENTLY_ACTIVE_THRESHOLD, LoopSpec, StemContext

log = logging.getLogger(__name__)

CARE_CHECK_INTERVAL = int(os.environ.get("CARE_CHECK_INTERVAL", "60"))
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "1800"))

MAX_RECENT_REMINDERS = 5  # how many past reminders to inject as dedup context
CARE_REMINDERS_KEY = "care.recent_reminders"  # NATS KV key for reminder history

KV_KEY = "identity"
_DEFAULT_IDENTITY_FALLBACK = "You are Maki."

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
    """Guard checks for the care loop: activity threshold, quiet hours, daily cap."""
    global _reminders_today, _reminders_today_date

    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    if time.time() - last_activity < RECENTLY_ACTIVE_THRESHOLD:
        return False

    if ctx.in_quiet_hours(config):
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    if today != _reminders_today_date:
        _reminders_today = 0
        _reminders_today_date = today

    max_reminders = config.get("max_reminders_per_day", 5)
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
    care_payload = {
        "turn_id": turn_id,
        "mode": "care",
        "identity": identity,
        "conversation": ctx.get_recent_conversation(),
        "memories": memories,
        "graph_context": graph_context,
        "prompt": None,
        "mission_results": None,
        "care_context": {
            "hours_since_last_interaction": round((time.time() - last_activity) / 3600, 1),
            "time_context": {
                "local_time": datetime.now().strftime("%H:%M"),
            },
            "recent_reminders": recent_reminders,
        },
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
    execution_interval_getter=lambda config: config.get("care_interval", 1800),
    should_run=_care_should_run,
    body=_care_body,
)
