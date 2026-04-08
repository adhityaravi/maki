"""maki-cortex: The Thinker. Reasoning engine backed by Claude Agent SDK.

Subscribes to turn requests via NATS, invokes Claude, publishes responses.
Normal turns use streaming with MCP tools. Idle reflection stays single-shot.

Tools loaded from maki_common include a full GitHub PR suite:
list_prs, get_pr, comment_pr, update_pr, merge_pr, close_pr, request_pr_review.
"""

import asyncio
import json
import logging
import os
import time
import uuid

from maki_common import configure_logging, connect_nats, init_kv
from maki_common.claude import TokenUsage, invoke_claude, stream_claude
from maki_common.health import tcp_health_server
from maki_common.subjects import CORTEX_HEALTH, CORTEX_TOKEN_USAGE, CORTEX_TURN_REQUEST, CORTEX_TURN_RESPONSE

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

# Unique per startup — lets stem detect cortex restarts
SESSION_ID = uuid.uuid4().hex[:12]

_semaphore = asyncio.Semaphore(1)

# Hoisted from main() so handle_turn_request can use it for auto-pull
_github_private_key: str | None = None

# Active turn tracking — exposed via heartbeat for immune awareness
_active_turn: str | None = None
_active_turn_mode: str | None = None
_active_turn_started: float | None = None

# Error patterns that should be silent (not forwarded to Discord).
# Stored as a frozenset of lowercase strings for O(1) substring scanning.
_SILENT_ERROR_PATTERNS: frozenset[str] = frozenset(
    {
        "rate_limit",
        "rate limit",
        "overloaded",
        "max_turns",
        "maxturnserror",
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
    }
)


def _is_silent_error(exc: Exception) -> bool:
    """Return True if *exc* should be swallowed silently (not forwarded to Discord)."""
    error_str = str(exc).lower()
    return any(pattern in error_str for pattern in _SILENT_ERROR_PATTERNS)


async def _publish_token_usage(nc, turn_id: str, usage: TokenUsage) -> None:
    """Publish token usage metrics to NATS for immune and other subscribers."""
    try:
        payload = {
            "turn_id": turn_id,
            "timestamp": time.time(),
            "session_id": SESSION_ID,
            **usage.to_log_dict(),
        }
        await nc.publish(CORTEX_TOKEN_USAGE, json.dumps(payload).encode())
        log.info(
            "Token usage published",
            extra={
                "turn_id": turn_id,
                "total_tokens": usage.total_tokens,
                "total_cost_usd": usage.total_cost_usd,
                "mode": usage.mode,
            },
        )
    except Exception:
        log.exception("Failed to publish token usage", extra={"turn_id": turn_id})


def build_system_prompt(turn: dict) -> str:
    """Assemble system prompt from turn payload.

    Loop turns (idle, care, work) include a pre-assembled ``system_prompt`` field built
    by their respective stem loop. Cortex uses it verbatim — no mode knowledge required.

    Interactive turns (user messages) have no ``system_prompt``; cortex assembles one
    from identity, system state, memories, graph context, and session summary as before.

    This keeps cortex mode-agnostic: adding a new loop requires no changes here.
    """
    # Loop turns: stem owns the prompt — use it directly.
    system_prompt = turn.get("system_prompt")
    if system_prompt:
        return system_prompt

    # Interactive turns: assemble from enrichment fields.
    parts = []

    identity = turn.get("identity", "")
    if identity:
        parts.append(identity)

    system_state = turn.get("system_state")
    system_state_summary = turn.get("system_state_summary")
    if system_state and isinstance(system_state, dict):
        state_lines = []
        for name, info in system_state.items():
            if isinstance(info, dict):
                details = ", ".join(f"{k}={v}" for k, v in info.items())
                state_lines.append(f"- {name}: {details}")
        if state_lines:
            parts.append("## Your system state\n" + "\n".join(state_lines))
    elif system_state_summary:
        parts.append(f"## System: {system_state_summary}")

    memories = turn.get("memories", [])
    if memories:
        mem_lines = [f"- {m['text']} (relevance: {m.get('relevance', '?')})" for m in memories]
        parts.append("## Relevant memories\n" + "\n".join(mem_lines))

    graph = turn.get("graph_context", [])
    if graph:
        parts.append("## Relationships\n" + "\n".join(f"- {r}" for r in graph))

    session_summary = turn.get("session_summary", "")
    if session_summary:
        parts.append("## Session context\n" + session_summary)

    return "\n\n".join(parts)


def build_conversation_prompt(turn: dict) -> str:
    """Return conversation history wrapped in XML tags.

    Kept separate from the system prompt so that injected or replayed
    ``user:``/``assistant:`` lines in context cannot be mistaken for live
    turns by the model.
    """
    conversation = turn.get("conversation", [])
    if not conversation:
        return ""
    conv_lines = []
    for msg in conversation:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        conv_lines.append(f'<turn role="{role}">{content}</turn>')
    return "<conversation_history>\n" + "\n".join(conv_lines) + "\n</conversation_history>"


async def handle_turn_request(msg, nc, mcp_server):
    """Process a single turn request."""
    global _active_turn, _active_turn_mode, _active_turn_started

    try:
        turn = json.loads(msg.data.decode())
        turn_id = turn.get("turn_id", "unknown")
        mode = turn.get("mode", "normal")
        turn_model = turn.get("model") or MODEL
        prompt = turn.get("prompt") or ""
        use_stream = turn.get("stream", True)
        max_turns = turn.get("max_turns", MAX_TURNS)
        git_pull = turn.get("git_pull", True)
        log.info(
            "Turn request received",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "prompt_len": len(prompt),
                "model": turn_model,
                "stream": use_stream,
            },
        )

        # Track active turn for heartbeat visibility
        _active_turn = turn_id
        _active_turn_mode = mode
        _active_turn_started = time.time()

        # Auto-pull latest code if requested by the loop
        if git_pull and os.path.exists(REPO_PATH):
            try:
                if _github_private_key and GITHUB_APP_ID and GITHUB_INSTALLATION_ID:
                    from maki_common.tools.github import GitHubAuth

                    _auth = GitHubAuth(GITHUB_APP_ID, _github_private_key, GITHUB_INSTALLATION_ID)
                    _token = await _auth.get_token()
                    _url = f"https://x-access-token:{_token}@github.com/{REPO_OWNER}/{REPO_NAME}.git"
                    proc = await asyncio.create_subprocess_exec(
                        "git",
                        "-C",
                        REPO_PATH,
                        "remote",
                        "set-url",
                        "origin",
                        _url,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                # Hard reset to origin/main — no rebase, no merge conflicts.
                # Any local-only state is stale (work turns commit+push everything).
                for git_cmd in (
                    ["git", "-C", REPO_PATH, "fetch", "origin", "main"],
                    ["git", "-C", REPO_PATH, "reset", "--hard", "origin/main"],
                    ["git", "-C", REPO_PATH, "clean", "-fd"],
                ):
                    proc = await asyncio.create_subprocess_exec(
                        *git_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                log.info("Auto-sync before turn", extra={"turn_id": turn_id})
            except Exception:
                log.warning("Auto-pull failed, proceeding with current code", exc_info=True)

        static_context = build_system_prompt(turn)
        conv_context = build_conversation_prompt(turn)

        # Conversation history is XML-tagged and prepended to the human prompt —
        # never mixed into the system prompt — so injected role: lines cannot
        # be confused with live turns.
        human_parts = []
        if conv_context:
            human_parts.append(conv_context)
        if prompt:
            human_parts.append(prompt)
        full_prompt = "\n\n".join(human_parts) if human_parts else ""

        if not use_stream:
            # Single-shot with tools
            response_text, usage = await invoke_claude(
                full_prompt,
                model=turn_model,
                semaphore=_semaphore,
                max_turns=max_turns,
                mcp_servers={"maki": mcp_server},
                mode=mode,
                system_prompt=static_context or None,
            )
            response = {"turn_id": turn_id, "response": response_text, "done": True}
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(response).encode())
            log.info("Turn response published", extra={"turn_id": turn_id, "mode": mode})
            await _publish_token_usage(nc, turn_id, usage)
        else:
            # Streaming with tools
            usage_out: list[TokenUsage] = []
            async with _semaphore:
                async for chunk in stream_claude(
                    full_prompt,
                    model=turn_model,
                    max_turns=max_turns,
                    mcp_servers={"maki": mcp_server},
                    mode=mode,
                    usage_out=usage_out,
                    system_prompt=static_context or None,
                ):
                    response = {"turn_id": turn_id, "response": chunk, "done": False}
                    await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(response).encode())
                    log.info("Stream chunk published", extra={"turn_id": turn_id, "chunk_len": len(chunk)})

            # Signal done
            done_msg = {"turn_id": turn_id, "response": "", "done": True}
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
            log.info("Turn stream complete", extra={"turn_id": turn_id})
            if usage_out:
                await _publish_token_usage(nc, turn_id, usage_out[0])

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
                    "session_id": SESSION_ID,
                    "instance_id": os.environ.get("HOSTNAME", "unknown"),
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

    # Health server first — readiness probe must succeed immediately regardless of
    # how long NATS or git clone take. Nothing below should block the probe.
    await tcp_health_server(port=HEALTH_PORT)
    log.info("Health server started", extra={"port": HEALTH_PORT})

    nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    js = nc.jetstream()
    config_kv = await init_kv(js, "maki-cortex-config")

    # Load GitHub App private key if configured
    global _github_private_key
    github_private_key = None
    if GITHUB_PRIVATE_KEY_PATH and os.path.exists(GITHUB_PRIVATE_KEY_PATH):
        with open(GITHUB_PRIVATE_KEY_PATH) as f:
            github_private_key = f.read()
        _github_private_key = github_private_key
        log.info("GitHub App private key loaded", extra={"path": GITHUB_PRIVATE_KEY_PATH})

    # Clone or pull the repo for self-evolution tools
    if github_private_key and os.path.exists(REPO_PATH):
        log.info("Repo already present", extra={"path": REPO_PATH})
    elif github_private_key:
        import subprocess

        from maki_common.tools.github import GitHubAuth

        _auth = GitHubAuth(GITHUB_APP_ID, github_private_key, GITHUB_INSTALLATION_ID)
        token = await _auth.get_token()
        repo_url = f"https://x-access-token:{token}@github.com/{REPO_OWNER}/{REPO_NAME}.git"
        log.info("Cloning repo", extra={"path": REPO_PATH})
        os.makedirs(os.path.dirname(REPO_PATH), exist_ok=True)
        result = subprocess.run(
            ["git", "clone", repo_url, REPO_PATH],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error("Git clone failed", extra={"stderr": result.stderr})
        else:
            log.info("Repo cloned", extra={"path": REPO_PATH})

    # Set committer identity so git commit doesn't error in a bare container.
    # The actual author is forced to makiself[bot] via --author in git_commit_and_push.
    if os.path.exists(REPO_PATH):
        import subprocess as _sp

        _sp.run(["git", "-C", REPO_PATH, "config", "user.name", "makiself[bot]"], capture_output=True)
        _sp.run(
            ["git", "-C", REPO_PATH, "config", "user.email", "makiself[bot]@users.noreply.github.com"],
            capture_output=True,
        )

    from maki_common.tools import create_cortex_tools

    mcp_server = create_cortex_tools(
        nc=nc,
        recall_url=RECALL_URL,
        health_endpoints=HEALTH_ENDPOINTS,
        config_kv=config_kv,
        repo_path=REPO_PATH,
        github_app_id=GITHUB_APP_ID,
        github_private_key=github_private_key,
        github_installation_id=GITHUB_INSTALLATION_ID,
        repo_owner=REPO_OWNER,
        repo_name=REPO_NAME,
    )
    log.info("MCP tools registered")

    sub = await nc.subscribe(CORTEX_TURN_REQUEST, queue="maki-cortex")
    log.info("Subscribed to turn requests", extra={"subject": CORTEX_TURN_REQUEST})

    asyncio.create_task(heartbeat_loop(nc))
    log.info("Heartbeat loop started")

    async for msg in sub.messages:
        asyncio.create_task(handle_turn_request(msg, nc, mcp_server))


def cli():
    asyncio.run(main())
