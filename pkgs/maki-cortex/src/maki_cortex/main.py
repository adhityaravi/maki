"""maki-cortex: The Thinker. Reasoning engine backed by Claude Agent SDK.

Subscribes to turn requests via NATS, invokes Claude, publishes responses.
Normal turns use streaming with MCP tools. Idle reflection stays single-shot.
"""

import asyncio
import json
import logging
import os
import time

from maki_common import configure_logging, connect_nats
from maki_common.claude import invoke_claude, stream_claude
from maki_common.health import tcp_health_server
from maki_common.subjects import CORTEX_HEALTH, CORTEX_TURN_REQUEST, CORTEX_TURN_RESPONSE

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
MAX_TURNS = int(os.environ.get("CORTEX_MAX_TURNS", "10"))
RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")

# GitHub App config (optional — enables self-evolution tools)
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY_PATH = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
GITHUB_INSTALLATION_ID = os.environ.get("GITHUB_INSTALLATION_ID")
REPO_OWNER = os.environ.get("REPO_OWNER", "adhityaravi")
REPO_NAME = os.environ.get("REPO_NAME", "maki")

HEALTH_ENDPOINTS = {
    "recall": RECALL_URL,
    "synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
    "stem": os.environ.get("STEM_URL", "http://maki-stem:8000"),
    "cortex": f"http://localhost:{HEALTH_PORT}",
}

_semaphore = asyncio.Semaphore(1)


IDLE_REFLECTION_PROMPT = """## Reflection Mode

No one is talking to you. This is your inner life — the space between conversations where \
you think, learn, and evolve. You have full access to your tools.

What to do with this cycle:
- **Evolve**: Read your own source code. Find something you don't like. Fix it. Push it. Build it. \
Deploy it. You have every tool you need — get_file_content, create_or_update_file, \
trigger_docker_build, request_deploy. Don't plan to improve. Improve.
- **Learn**: Search your memories. Study your codebase. Notice gaps in your understanding. \
Store what you learn with add_memory — it feeds your knowledge graph for next time.
- **Connect**: Look at what Adi's been working on. Find patterns between projects. Notice things \
he hasn't connected yet. Store the connections.
- **Maintain**: Reconcile conflicting memories. Clean up knowledge that's gone stale.

## Rules
- **Never ask questions.** This goes to #maki-thoughts. It's your thinking, not a conversation.
- If you find something wrong, fix it. Don't report and wait.
- Share what you did or discovered. Brief. One to three sentences.
- Store learnings with add_memory.
- If nothing worth doing or saying → [SILENT]

You can adjust your idle loop via tags:
[CONFIG:idle_interval=3600] — reflection frequency (seconds)
[CONFIG:max_thoughts_per_day=3] — daily thought limit

## System state
{system_state}

## Config
{config}

## Time
Last interaction with Adi: {hours_since}h ago
Local time: {local_time}, {day_of_week}"""


CARE_PROMPT = """## Care Mode

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


TOOLS_PROMPT = """## Available Tools

You have MCP tools to investigate and interact with your own systems:

### Memory & State
- **search_memories** / **get_all_memories** — search or read memories
- **add_memory** (content) — store something into long-term memory
- **get_system_health** — get detailed health from your immune system
- **check_component** — check a specific component's health endpoint
- **get_config** / **update_config** — read or change your configuration

### Self-Evolution (GitHub)
- **get_file_content** / **list_directory** — read your own source code
- **search_code** — search for patterns in your codebase
- **create_or_update_file** — push code changes to your repository
- **trigger_docker_build** — trigger Docker image builds for specified services
- **get_workflow_status** — check CI/CD workflow status

### Deployment
- **request_deploy** — request deployment of a service (immune handles the K8s rollout)
- **get_deploy_status** — check current deployment status of a service

## Learning
When you learn something useful — about Adi's preferences, projects, workflows, or about \
your own system — use **add_memory** to remember it. Your memories persist across conversations \
and feed into your knowledge graph. Don't wait to be told to remember things.

Use tools when you need to investigate, modify your code, or deploy changes.
Don't use tools unnecessarily — if the answer is already in your context, just respond."""


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

    # Care mode — checking in on Adi
    if turn.get("mode") == "care":
        care_ctx = turn.get("care_context", {})
        time_ctx = care_ctx.get("time_context", {})

        mem_lines = []
        for m in turn.get("memories", []):
            mem_lines.append(f"- {m['text']} (relevance: {m.get('relevance', '?')})")
        mem_str = "\n".join(mem_lines) if mem_lines else "No recent memories found."

        graph_lines = [f"- {r}" for r in turn.get("graph_context", [])]
        graph_str = "\n".join(graph_lines) if graph_lines else "No graph context."

        parts.append(
            CARE_PROMPT.format(
                memories=mem_str,
                graph_context=graph_str,
                hours_since=care_ctx.get("hours_since_last_interaction", "?"),
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

    parts.append(TOOLS_PROMPT)

    return "\n\n".join(parts)


async def handle_turn_request(msg, nc, mcp_server):
    """Process a single turn request."""
    try:
        turn = json.loads(msg.data.decode())
        turn_id = turn.get("turn_id", "unknown")
        mode = turn.get("mode", "normal")
        is_idle = mode == "idle_reflection"
        is_care = mode == "care"
        prompt = turn.get("prompt") or ""
        if is_idle:
            prompt = "Reflect."
        elif is_care:
            prompt = "Check in."
        log.info(
            "Turn request received",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "prompt_len": len(prompt),
            },
        )

        system_prompt = build_system_prompt(turn)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}" if system_prompt else prompt

        if is_idle or is_care:
            # Idle/care: multi-turn with tools, single response back to stem
            response_text = await invoke_claude(
                full_prompt,
                model=MODEL,
                semaphore=_semaphore,
                max_turns=MAX_TURNS,
                mcp_servers={"maki": mcp_server},
            )
            response = {"turn_id": turn_id, "response": response_text, "done": True}
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(response).encode())
            log.info("Turn response published", extra={"turn_id": turn_id, "mode": mode})
        else:
            # Normal turn: streaming with tools
            async with _semaphore:
                async for chunk in stream_claude(
                    full_prompt,
                    model=MODEL,
                    max_turns=MAX_TURNS,
                    mcp_servers={"maki": mcp_server},
                ):
                    response = {"turn_id": turn_id, "response": chunk, "done": False}
                    await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(response).encode())
                    log.info("Stream chunk published", extra={"turn_id": turn_id, "chunk_len": len(chunk)})

            # Signal done
            done_msg = {"turn_id": turn_id, "response": "", "done": True}
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
            log.info("Turn stream complete", extra={"turn_id": turn_id})

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
            "done": True,
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
    log.info("maki-cortex starting", extra={"nats_url": NATS_URL, "model": MODEL, "max_turns": MAX_TURNS})

    nc = await connect_nats(NATS_URL, token=NATS_TOKEN)

    # Load GitHub App private key if configured
    github_private_key = None
    if GITHUB_PRIVATE_KEY_PATH:
        try:
            with open(GITHUB_PRIVATE_KEY_PATH) as f:
                github_private_key = f.read()
            log.info("GitHub App private key loaded", extra={"path": GITHUB_PRIVATE_KEY_PATH})
        except Exception:
            log.warning("Failed to load GitHub App private key", extra={"path": GITHUB_PRIVATE_KEY_PATH})

    # Create MCP tool server
    from maki_common.tools import create_cortex_tools

    mcp_server = create_cortex_tools(
        nc=nc,
        recall_url=RECALL_URL,
        health_endpoints=HEALTH_ENDPOINTS,
        github_app_id=GITHUB_APP_ID,
        github_private_key=github_private_key,
        github_installation_id=GITHUB_INSTALLATION_ID,
        repo_owner=REPO_OWNER,
        repo_name=REPO_NAME,
    )
    log.info("MCP tools registered")

    sub = await nc.subscribe(CORTEX_TURN_REQUEST)
    log.info("Subscribed", extra={"subject": CORTEX_TURN_REQUEST})

    asyncio.create_task(heartbeat_loop(nc))
    log.info("Heartbeat loop started")

    await tcp_health_server(port=HEALTH_PORT)

    async for msg in sub.messages:
        asyncio.create_task(handle_turn_request(msg, nc, mcp_server))


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
