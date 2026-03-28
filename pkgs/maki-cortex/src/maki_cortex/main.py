"""maki-cortex: The Thinker. Reasoning engine backed by Claude Agent SDK.

Subscribes to turn requests via NATS, invokes Claude, publishes responses.
"""

import asyncio
import json
import logging
import os
import time

from maki_common import configure_logging, connect_nats
from maki_common.claude import invoke_claude
from maki_common.health import tcp_health_server
from maki_common.subjects import CORTEX_HEALTH, CORTEX_TURN_REQUEST, CORTEX_TURN_RESPONSE

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))

_semaphore = asyncio.Semaphore(1)


IDLE_REFLECTION_PROMPT = """## Reflection Mode

You are in idle reflection mode. There is no user message — this is your inner life.
You have context about recent memories, relationships, and your own system state.

Decide what to do with this cycle. You can:
- **Curiosity**: Research topics Adi is working on, find connections between projects
- **Care**: Follow up on things mentioned, notice if something seems stuck or stressful
- **Maintenance**: Reconcile conflicting memories, identify knowledge gaps
- **Anticipation**: Prep insights based on patterns you notice
- **Self-improvement**: Identify weaknesses in your reasoning, notice what you get wrong

If you have a thought, just write it naturally. Keep it concise.
If you have nothing to say, respond with exactly: [SILENT]

You can also adjust your own idle loop configuration by including tags like:
[CONFIG:idle_interval=3600] to change how often you reflect (in seconds)
[CONFIG:max_thoughts_per_day=3] to limit your daily thoughts

## Your system state
{system_state}

## Current config
{config}

## Time context
Last interaction with Adi: {hours_since}h ago
Local time: {local_time}, {day_of_week}"""


def build_system_prompt(turn: dict) -> str:
    """Assemble system prompt from identity, memories, and graph context."""
    parts = []

    identity = turn.get("identity", "")
    if identity:
        parts.append(identity)

    # Idle reflection mode — add reflection context
    if turn.get("mode") == "idle_reflection":
        idle_ctx = turn.get("idle_context", {})
        time_ctx = idle_ctx.get("time_context", {})
        system_state = idle_ctx.get("system_state", {})
        config = idle_ctx.get("current_config", {})

        state_lines = []
        for name, info in system_state.items():
            if isinstance(info, dict):
                details = ", ".join(f"{k}={v}" for k, v in info.items())
                state_lines.append(f"- {name}: {details}")
        state_str = "\n".join(state_lines) if state_lines else "No data available"

        config_str = "\n".join(f"- {k}: {v}" for k, v in config.items())

        parts.append(
            IDLE_REFLECTION_PROMPT.format(
                system_state=state_str,
                config=config_str,
                hours_since=idle_ctx.get("hours_since_last_interaction", "?"),
                local_time=time_ctx.get("local_time", "?"),
                day_of_week=time_ctx.get("day_of_week", "?"),
            )
        )

    # System state — available in all turns for self-awareness
    system_state = turn.get("system_state") or (turn.get("idle_context", {}).get("system_state"))
    if system_state and turn.get("mode") != "idle_reflection":
        state_lines = []
        for name, info in system_state.items():
            if isinstance(info, dict):
                details = ", ".join(f"{k}={v}" for k, v in info.items())
                state_lines.append(f"- {name}: {details}")
        if state_lines:
            parts.append("## Your system state\n" + "\n".join(state_lines))

    memories = turn.get("memories", [])
    if memories:
        mem_lines = [f"- {m['text']} (relevance: {m.get('relevance', '?')})" for m in memories]
        parts.append("## Relevant memories\n" + "\n".join(mem_lines))

    graph = turn.get("graph_context", [])
    if graph:
        parts.append("## Relationships\n" + "\n".join(f"- {r}" for r in graph))

    conversation = turn.get("conversation", [])
    if conversation:
        conv_lines = []
        for msg in conversation:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            conv_lines.append(f"{role}: {content}")
        parts.append("## Recent conversation\n" + "\n".join(conv_lines))

    return "\n\n".join(parts)


async def handle_turn_request(msg, nc):
    """Process a single turn request."""
    try:
        turn = json.loads(msg.data.decode())
        turn_id = turn.get("turn_id", "unknown")
        is_idle = turn.get("mode") == "idle_reflection"
        prompt = turn.get("prompt") or ""
        if is_idle:
            prompt = "Reflect."
        log.info(
            "Turn request received",
            extra={
                "turn_id": turn_id,
                "mode": "idle_reflection" if is_idle else "normal",
                "prompt_len": len(prompt),
            },
        )

        system_prompt = build_system_prompt(turn)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}" if system_prompt else prompt
        response_text = await invoke_claude(full_prompt, model=MODEL, semaphore=_semaphore)

        response = {
            "turn_id": turn_id,
            "response": response_text,
            "mission_proposals": [],
        }

        await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(response).encode())
        log.info("Turn response published", extra={"turn_id": turn_id})

    except Exception:
        log.exception("Error handling turn request")
        turn_id = "unknown"
        try:
            turn_id = json.loads(msg.data.decode()).get("turn_id", "unknown")
        except Exception:
            pass
        error_response = {
            "turn_id": turn_id,
            "response": "I encountered an error processing this turn. Please try again.",
            "mission_proposals": [],
        }
        await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(error_response).encode())


async def heartbeat_loop(nc):
    """Publish periodic heartbeat."""
    while True:
        try:
            payload = json.dumps(
                {
                    "status": "ok",
                    "timestamp": time.time(),
                    "model": MODEL,
                }
            ).encode()
            await nc.publish(CORTEX_HEALTH, payload)
        except Exception:
            log.exception("Heartbeat publish failed")
        await asyncio.sleep(15)


async def main():
    log.info("maki-cortex starting", extra={"nats_url": NATS_URL, "model": MODEL})

    nc = await connect_nats(NATS_URL)

    sub = await nc.subscribe(CORTEX_TURN_REQUEST)
    log.info("Subscribed", extra={"subject": CORTEX_TURN_REQUEST})

    asyncio.create_task(heartbeat_loop(nc))
    log.info("Heartbeat loop started")

    await tcp_health_server(port=HEALTH_PORT)

    async for msg in sub.messages:
        asyncio.create_task(handle_turn_request(msg, nc))


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
