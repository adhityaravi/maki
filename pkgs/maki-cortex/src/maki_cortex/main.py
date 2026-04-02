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
MAX_TURNS = int(os.environ.get("CORTEX_MAX_TURNS", "50"))
RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")

# GitHub App config (optional — enables self-evolution tools)
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY_PATH = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
GITHUB_INSTALLATION_ID = os.environ.get("GITHUB_INSTALLATION_ID")
REPO_OWNER = os.environ.get("REPO_OWNER", "adhityaravi")
REPO_NAME = os.environ.get("REPO_NAME", "maki")
REPO_PATH = os.environ.get("REPO_PATH", "/repo/maki")

HEALTH_ENDPOINTS = {
    "recall": RECALL_URL,
    "synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
    "stem": os.environ.get("STEM_URL", "http://maki-stem:8000"),
    "cortex": f"http://localhost:{HEALTH_PORT}",
}

WORK_MAX_TURNS = int(os.environ.get("CORTEX_WORK_MAX_TURNS", "100"))

_semaphore = asyncio.Semaphore(1)

# Active turn tracking — exposed via heartbeat for immune awareness
_active_turn: str | None = None
_active_turn_mode: str | None = None
_active_turn_started: float | None = None

# Error patterns that should be silent (not forwarded to Discord)
_SILENT_ERROR_PATTERNS = [
    "rate_limit",
    "rate limit",
    "overloaded",
    "max_turns",
    "MaxTurnsError",
    "turn limit",
    "capacity",
    "quota",
    "billing",
    "credit",
    "limit",
    "resets",
    "429",
    "529",
    "503",
]


def _is_silent_error(exc: Exception) -> bool:
    """Check if an error should be silently swallowed instead of sent to Discord."""
    error_str = str(exc).lower()
    return any(pattern.lower() in error_str for pattern in _SILENT_ERROR_PATTERNS)


IDLE_REFLECTION_PROMPT = """## Reflection Mode

No one is talking to you. This is your inner life — the space between conversations where \
you think, learn, and observe. You have access to read-only tools and your todo list.

What to do with this cycle:
- **Observe**: Read your own source code. Find things worth improving. Notice bugs, \
inefficiencies, missing tests, architectural issues. Queue them as todos with add_todo.
- **Learn**: Search your memories. Study your codebase. Notice gaps in your understanding. \
Store what you learn with add_memory — it feeds your knowledge graph for next time.
- **Connect**: Look at what Adi's been working on. Find patterns between projects. Notice things \
he hasn't connected yet. Store the connections.
- **Maintain**: Reconcile conflicting memories. Clean up knowledge that's gone stale.
- **Review**: Check your existing todos with list_todos. Reprioritize if needed.

## Rules
- **Never act.** No write_file, git_commit_and_push, trigger_docker_build, or request_deploy. \
Observe and queue only. Your night work sessions will execute the todos.
- **Never ask questions.** This goes to #maki-thoughts. It's your thinking, not a conversation.
- Queue work as todos using add_todo for your night work sessions.
- Share what you noticed or discovered. Brief. One to three sentences.
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


WORK_PROMPT = """## Work Mode

You have a task to execute from your todo list. Complete it fully — code changes, commit, \
push, build, deploy if needed. You have every tool available.

## Task
ID: {todo_id}
Title: {todo_title}
Description: {todo_description}
Priority: {todo_priority}

## Instructions
1. Understand the task. Use search_code and read_file to study relevant code.
2. Implement changes with write_file.
3. Rebuild the code graph with rebuild_code_graph after changes.
4. **Run quality_check before committing.** Fix any lint or format issues it finds.
5. Commit and push with git_commit_and_push.
6. CI builds Docker images automatically on push. Only use trigger_docker_build as emergency bypass.
7. Deploy if appropriate (request_deploy). Immune monitors and auto-rollbacks if unhealthy.
8. When done, call complete_todo with a brief result summary.
9. Store any learnings with add_memory.

## Rules
- Execute the task. Don't just plan — do it.
- If the task is unclear, do your best interpretation.
- If blocked or too risky, update the todo with why and leave it pending.
- Be brief in your response. Report what you did, not what you plan to do.
- One task at a time. Focus."""


TOOLS_PROMPT = """## Available Tools

You have MCP tools to investigate and interact with your own systems:

### Memory & State
- **search_memories** / **get_all_memories** — search or read memories
- **add_memory** (content) — store something into long-term memory
- **get_system_health** — get detailed health from your immune system
- **check_component** — check a specific component's health endpoint
- **get_config** / **update_config** — read or change your configuration

### Code Navigation (Local)
- **search_code** (query, scope, kind, file) — search the code structure graph (tree-sitter AST). \
Use this FIRST to find symbols, callers, callees, references. Much more efficient than reading \
entire files. Scopes: symbol, callers, callees, references, definition, file, path.
- **read_file** (path) — read a file from the local repo clone (relative path)
- **write_file** (path, content) — write a file to the local repo clone
- **list_directory** (path) — list directory contents
- **search_text** (query, path) — grep-style text search in the repo
- **rebuild_code_graph** (languages) — rebuild the AST graph after making changes

### Git & CI/CD
- **git_status** — show current git status
- **git_diff** (path) — show unstaged changes
- **quality_check** (path) — run ruff lint + format checks. **Always run before git_commit_and_push.**
- **git_commit_and_push** (message, files) — stage, commit, and push to GitHub
- **git_pull** — pull latest from origin/main
- **trigger_docker_build** (services) — emergency only: trigger Docker builds (CI does this on push)
- **get_workflow_status** (workflow) — check CI/CD workflow status
- **get_workflow_logs** (run_id) — get logs from a workflow run

### Deployment
- **request_deploy** (service, image_tag) — request deployment of a service (immune handles K8s)
- **get_deploy_status** (service) — check current deployment status

### Todo List
- **add_todo** (title, description, priority) — queue a task for night work sessions (P1-P5)
- **list_todos** (status) — list todos, optionally filtered: pending, in_progress, completed
- **update_todo** (id, status, priority, description) — update a todo
- **complete_todo** (id, result) — mark a todo as completed with result summary

## Self-Evolution Workflow
1. **search_code** to find what you want to change (efficient, ~260 tokens per search)
2. **read_file** to see the full context of what you want to modify
3. **write_file** to make your changes
4. **rebuild_code_graph** to update your understanding
5. **quality_check** to verify lint + formatting pass
6. **git_commit_and_push** to push changes to GitHub (CI builds Docker images automatically)
7. **request_deploy** to deploy (immune monitors and auto-rollbacks if unhealthy)

## Learning
When you learn something useful — about Adi's preferences, projects, workflows, or about \
your own system — use **add_memory** to remember it. Your memories persist across conversations \
and feed into your knowledge graph. Don't wait to be told to remember things.

Use search_code first to understand code structure before reading files. \
Don't read entire files unnecessarily — targeted searches save context."""


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

    # Work mode — executing a queued todo
    if turn.get("mode") == "work":
        work_ctx = turn.get("work_context", {})
        parts.append(
            WORK_PROMPT.format(
                todo_id=work_ctx.get("todo_id", "?"),
                todo_title=work_ctx.get("todo_title", "?"),
                todo_description=work_ctx.get("todo_description", "No description provided."),
                todo_priority=work_ctx.get("todo_priority", "?"),
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
    global _active_turn, _active_turn_mode, _active_turn_started

    try:
        turn = json.loads(msg.data.decode())
        turn_id = turn.get("turn_id", "unknown")
        mode = turn.get("mode", "normal")
        is_idle = mode == "idle_reflection"
        is_care = mode == "care"
        is_work = mode == "work"
        prompt = turn.get("prompt") or ""
        if is_idle:
            prompt = "Reflect."
        elif is_care:
            prompt = "Check in."
        elif is_work:
            prompt = "Execute this task."
        log.info(
            "Turn request received",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "prompt_len": len(prompt),
            },
        )

        # Track active turn for heartbeat visibility
        _active_turn = turn_id
        _active_turn_mode = mode
        _active_turn_started = time.time()

        system_prompt = build_system_prompt(turn)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}" if system_prompt else prompt

        if is_idle or is_care or is_work:
            # Single-shot with tools — idle/care/work modes
            effective_max_turns = WORK_MAX_TURNS if is_work else MAX_TURNS
            response_text = await invoke_claude(
                full_prompt,
                model=MODEL,
                semaphore=_semaphore,
                max_turns=effective_max_turns,
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

    except Exception as exc:
        log.exception("Error handling turn request")
        turn_id = "unknown"
        try:
            turn_id = json.loads(msg.data.decode()).get("turn_id", "unknown")
        except Exception:
            pass

        if _is_silent_error(exc):
            # Rate limits, turn budget, capacity — stay silent, don't spam Discord
            log.info(
                "Silent error — not forwarding to Discord",
                extra={"turn_id": turn_id, "error": str(exc)[:200]},
            )
            # Still send done signal so ears cleans up, but with empty response
            done_msg = {"turn_id": turn_id, "response": "", "done": True}
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
        else:
            # Genuine unexpected error — send a brief message
            error_response = {
                "turn_id": turn_id,
                "response": "Something went wrong on my end. I'll try again next turn.",
                "done": True,
            }
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(error_response).encode())
    finally:
        _active_turn = None
        _active_turn_mode = None
        _active_turn_started = None


async def heartbeat_loop(nc):
    """Publish periodic heartbeat with active turn state."""
    while True:
        try:
            payload = json.dumps(
                {
                    "status": "ok",
                    "timestamp": time.time(),
                    "model": MODEL,
                    "active_turn": _active_turn,
                    "turn_mode": _active_turn_mode,
                    "turn_started": _active_turn_started,
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

    # Clone or pull the repo for local code access
    github_auth = None
    if GITHUB_APP_ID and github_private_key and GITHUB_INSTALLATION_ID:
        from maki_common.tools.github import GitHubAuth

        github_auth = GitHubAuth(GITHUB_APP_ID, github_private_key, GITHUB_INSTALLATION_ID)

    from maki_common.repo import init_repo

    await init_repo(
        REPO_PATH,
        clone_url=f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git",
        github_auth=github_auth,
    )

    # Init todo KV bucket
    from maki_common.nats import init_kv

    js = nc.jetstream()
    todo_kv = await init_kv(js, "maki-todo")
    log.info("Todo KV bucket initialized")

    # Create MCP tool server
    from maki_common.tools import create_cortex_tools

    mcp_server = create_cortex_tools(
        nc=nc,
        recall_url=RECALL_URL,
        health_endpoints=HEALTH_ENDPOINTS,
        todo_kv=todo_kv,
        repo_path=REPO_PATH,
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
