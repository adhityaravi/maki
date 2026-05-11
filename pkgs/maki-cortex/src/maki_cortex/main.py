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

from maki_common import configure_logging, connect_nats, init_kv, subscribe_supervised
from maki_common.claude import TokenUsage, invoke_claude, stream_claude
from maki_common.health import tcp_health_server
from maki_common.repo import redact_token
from maki_common.subjects import CORTEX_HEALTH, CORTEX_TOKEN_USAGE, CORTEX_TURN_REQUEST, CORTEX_TURN_RESPONSE

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")
SITE_NAME = os.environ.get("SITE_NAME", "unknown")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
MAX_TURNS = int(os.environ.get("CORTEX_MAX_TURNS", "50"))
RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")

# Hard turn-duration watchdog. If a turn doesn't return within this window we
# cancel it from inside the cortex so _active_turn state is cleared, slot
# semaphores are released, and a `cancelled=True` done signal is published.
# Without this, a hung invoke_claude / stream_claude (network stall, SDK
# livelock, uncancellable native call) pins the cortex forever — the heartbeat
# stays "healthy" (it runs in a separate task), and external stuck detection
# only fires if the original submitter is still waiting. See issue #150.
CORTEX_MAX_TURN_SECONDS = int(os.environ.get("CORTEX_MAX_TURN_SECONDS", "1200"))

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
_active_task: asyncio.Task | None = None  # for preemption: cancel background turns

# Health-check inputs — populated as startup progresses. The /health endpoint
# returns 503 until all of these are wired so kubelet readiness probes
# accurately reflect "ready to handle turns".
_nc_ref = None
_heartbeat_task: asyncio.Task | None = None
# Critical listener tasks. They're wrapped in ``subscribe_supervised`` so
# they should never exit cleanly on their own — if any of them is ``done()``
# the readiness probe must flip red so kubelet restarts the pod (issue #175).
_critical_listener_tasks: dict[str, asyncio.Task] = {}


# Liveness escape hatch: if a turn has been "running" for more than this
# multiple of the soft watchdog, fail the health probe so kubelet SIGKILLs
# the pod. This is the only way out when the asyncio loop is wedged below
# the application layer (uncancellable native call, sync-blocking work
# inside a turn task) — `asyncio.wait_for` can't fire if the loop never
# reaches a suspension point. See issue #185.
CORTEX_LIVENESS_TURN_MULTIPLIER = float(os.environ.get("CORTEX_LIVENESS_TURN_MULTIPLIER", "2.0"))


def _health_check() -> tuple[bool, str | None]:
    """Return (ok, reason) for the readiness/liveness probe.

    On top of the usual NATS / subscription / heartbeat checks, fail the
    probe when the active turn has been running far longer than the soft
    watchdog allows. The soft watchdog (asyncio.wait_for inside
    handle_turn_request) only fires at suspension points; if the event
    loop is wedged below the application layer the only recovery is to
    let kubelet SIGKILL the pod. See issue #185.
    """
    if _nc_ref is None or not _nc_ref.is_connected:
        return False, "NATS not connected"
    if _heartbeat_task is None:
        return False, "Heartbeat task not started"
    if _heartbeat_task.done():
        if _heartbeat_task.cancelled():
            return False, "Heartbeat task cancelled"
        exc = _heartbeat_task.exception()
        return False, f"Heartbeat task crashed: {exc!r}"
    # Critical listeners are wrapped in ``subscribe_supervised`` and should
    # run forever. If any has exited, the readiness probe must fail so kubelet
    # restarts the pod (issue #175).
    if not _critical_listener_tasks:
        return False, "Turn-request listener not started"
    for label, task in _critical_listener_tasks.items():
        if task.done():
            if task.cancelled():
                return False, f"{label} listener cancelled"
            exc = task.exception()
            return False, f"{label} listener crashed: {exc!r}"

    # Hard liveness escape: if a turn is wedged way past the soft watchdog
    # window, kubelet must restart us. This is the only way out of an
    # uncancellable native call or a wedged event loop.
    if _active_turn_started is not None:
        running_s = time.time() - _active_turn_started
        liveness_threshold = CORTEX_LIVENESS_TURN_MULTIPLIER * CORTEX_MAX_TURN_SECONDS
        if running_s > liveness_threshold:
            return (
                False,
                f"Turn {_active_turn} wedged for {running_s:.0f}s "
                f"(> {liveness_threshold:.0f}s = {CORTEX_LIVENESS_TURN_MULTIPLIER}x watchdog) — "
                f"requesting kubelet restart",
            )

    return True, None


_BACKGROUND_MODES = frozenset({"idle_reflection", "work", "care", "trading_analyst"})

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
        # "limit" removed — too broad, already covered by specific rate-limit patterns above
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
        await nc.publish(f"{CORTEX_TOKEN_USAGE}.{SITE_NAME}", json.dumps(payload).encode())
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


async def _process_turn(turn: dict, turn_id: str, mode: str, nc, mcp_server) -> None:
    """Inner turn processing — wrapped with asyncio.wait_for for the timeout watchdog.

    Kept as a separate coroutine so a hard turn-duration timeout can cancel the
    whole pipeline (auto-pull, prompt assembly, claude invocation/streaming) in
    one place. See ``handle_turn_request``.
    """
    turn_model = turn.get("model") or MODEL
    prompt = turn.get("prompt") or ""
    use_stream = turn.get("stream", True)
    max_turns = turn.get("max_turns", MAX_TURNS)
    git_pull = turn.get("git_pull", True)

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
                _stdout, _stderr = await proc.communicate()
                if proc.returncode != 0:
                    # Redact: git error messages echo the full remote URL,
                    # which contains the installation token.
                    log.warning(
                        "Auto-sync git command failed",
                        extra={
                            "cmd": git_cmd[3:],
                            "stderr": redact_token(_stderr.decode(errors="replace")),
                        },
                    )
            log.info("Auto-sync before turn", extra={"turn_id": turn_id})
            # Invalidate code graph cache — files on disk changed
            from maki_common.tools.codegraph_tools import invalidate_graph_cache

            invalidate_graph_cache(REPO_PATH)
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
        # Single-shot with tools. Timing brackets isolate hangs to the
        # Claude invocation itself (vs prompt assembly / git auto-pull).
        # Idle reflection takes this path (stream=False) and is the
        # specific case wedging the cortex per issue #185.
        log.info(
            "Invoking Claude (single-shot)",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "model": turn_model,
                "prompt_len": len(full_prompt),
                "system_prompt_len": len(static_context or ""),
            },
        )
        invoke_started = time.monotonic()
        response_text, usage = await invoke_claude(
            full_prompt,
            model=turn_model,
            semaphore=_semaphore,
            max_turns=max_turns,
            mcp_servers={"maki": mcp_server},
            mode=mode,
            system_prompt=static_context or None,
        )
        log.info(
            "Claude invocation complete",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "duration_s": round(time.monotonic() - invoke_started, 2),
                "response_len": len(response_text or ""),
            },
        )
        response = {"turn_id": turn_id, "response": response_text, "done": True}
        await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(response).encode())
        log.info("Turn response published", extra={"turn_id": turn_id, "mode": mode})
        await _publish_token_usage(nc, turn_id, usage)
    else:
        # Streaming with tools
        log.info(
            "Invoking Claude (stream)",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "model": turn_model,
                "prompt_len": len(full_prompt),
                "system_prompt_len": len(static_context or ""),
            },
        )
        stream_started = time.monotonic()
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
        log.info(
            "Turn stream complete",
            extra={
                "turn_id": turn_id,
                "duration_s": round(time.monotonic() - stream_started, 2),
            },
        )
        if usage_out:
            await _publish_token_usage(nc, turn_id, usage_out[0])


async def handle_turn_request(msg, nc, mcp_server):
    """Process a single turn request with a hard turn-duration watchdog.

    The whole pipeline runs inside ``asyncio.wait_for`` so a hung
    ``invoke_claude`` / ``stream_claude`` (network stall, SDK livelock,
    uncancellable subprocess) cannot pin the cortex forever. On timeout:
    cancel the body, log, publish a ``cancelled=True`` done signal so the
    submitter is unblocked, and let ``finally`` clear ``_active_turn`` state
    so the next turn can run. Heartbeat is in a separate task and stays
    healthy throughout — without this, only an external CORTEX_STUCK signal
    (from a still-waiting submitter) could ever recover the pod. See #150.
    """
    global _active_turn, _active_turn_mode, _active_turn_started, _active_task

    # First-line breadcrumb: emitted *before* any work so we can prove from
    # logs alone whether the dispatcher is even entering the handler. If
    # this line is missing for a wedged turn, the NATS dispatch into
    # handle_turn_request is starved (event loop blocked from a prior
    # task). If it's present but "Turn request received" is not, the
    # blockage is between this point and the JSON parse / log call below
    # (extremely unlikely, but worth distinguishing). See issue #185.
    log.info("Turn handler entered", extra={"data_len": len(msg.data) if msg.data else 0})

    # Parse turn id / mode early so every error path can reference them.
    turn_id = "unknown"
    mode = "unknown"
    try:
        turn = json.loads(msg.data.decode())
        turn_id = turn.get("turn_id", "unknown")
        mode = turn.get("mode", "normal")
    except Exception:
        log.exception("Failed to parse turn request — dropping")
        return

    prompt_len = len(turn.get("prompt") or "")
    turn_model = turn.get("model") or MODEL
    use_stream = turn.get("stream", True)
    log.info(
        "Turn request received",
        extra={
            "turn_id": turn_id,
            "mode": mode,
            "prompt_len": prompt_len,
            "model": turn_model,
            "stream": use_stream,
            "timeout_s": CORTEX_MAX_TURN_SECONDS,
        },
    )

    # Track active turn for heartbeat visibility.
    _active_turn = turn_id
    _active_turn_mode = mode
    _active_turn_started = time.time()

    try:
        await asyncio.wait_for(
            _process_turn(turn, turn_id, mode, nc, mcp_server),
            timeout=CORTEX_MAX_TURN_SECONDS,
        )

    except TimeoutError:
        # Hard watchdog fired. _process_turn has already been cancelled by
        # wait_for; we just need to log and unblock the submitter.
        log.error(
            "Turn exceeded hard timeout — cancelling",
            extra={
                "turn_id": turn_id,
                "mode": mode,
                "timeout_s": CORTEX_MAX_TURN_SECONDS,
            },
        )
        try:
            done_msg = {
                "turn_id": turn_id,
                "response": "",
                "done": True,
                "cancelled": True,
                "reason": "cortex_turn_timeout",
            }
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
        except Exception:
            log.exception("Failed to publish timeout done signal", extra={"turn_id": turn_id})

    except asyncio.CancelledError:
        log.info("Turn cancelled by preemption", extra={"turn_id": turn_id, "mode": mode})
        done_msg = {"turn_id": turn_id, "response": "", "done": True, "cancelled": True}
        try:
            await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
        except Exception:
            log.exception("Failed to publish preemption done signal", extra={"turn_id": turn_id})
        # Re-raise so the cancelling caller sees the cancellation propagate.
        raise

    except Exception as exc:
        log.exception("Error handling turn request", extra={"turn_id": turn_id})

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
        _active_task = None


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
    log.info(
        "maki-cortex starting",
        extra={"nats_url": NATS_URL, "model": MODEL, "max_turns": MAX_TURNS, "session_id": SESSION_ID},
    )

    # Health server up front so kubelet probes can connect from the moment
    # the pod starts. The check returns 503 until NATS, the turn subscription
    # and the heartbeat task are all live, which keeps readiness false during
    # startup (no traffic routed) without killing the pod via liveness.
    await tcp_health_server(port=HEALTH_PORT, check=_health_check)
    log.info("Health server started", extra={"port": HEALTH_PORT})

    global _nc_ref, _heartbeat_task
    nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _nc_ref = nc
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
            # Redact: git's own clone error includes the auth URL, which carries
            # a live installation token. Logs are forever; tokens shouldn't be.
            log.error("Git clone failed", extra={"stderr": redact_token(result.stderr)})
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

    _heartbeat_task = asyncio.create_task(heartbeat_loop(nc))
    log.info("Heartbeat loop started")

    async def _handle_turn_message(msg) -> None:
        """Dispatch one CORTEX_TURN_REQUEST message into a background task.

        Kept narrow: preemption logic and per-turn task tracking stay here,
        while the supervised loop owns the subscribe/iterate lifecycle so a
        silent stream drain re-subscribes instead of leaving cortex blind to
        new turns (issue #175).
        """
        global _active_task
        try:
            turn = json.loads(msg.data.decode())
            mode = turn.get("mode", "")
        except Exception:
            mode = ""

        is_background = mode in _BACKGROUND_MODES

        # Preempt: interactive turn cancels running background turn
        if not is_background and _active_task and _active_turn_mode in _BACKGROUND_MODES:
            _active_task.cancel()
            log.info("Preempted background turn", extra={"cancelled_mode": _active_turn_mode})

        task = asyncio.create_task(handle_turn_request(msg, nc, mcp_server))
        if is_background:
            _active_task = task

    # Critical: the turn-request listener is the entire point of cortex. Wrap
    # it in ``subscribe_supervised`` so a NATS reconnect or stream drain
    # re-subscribes instead of silently exiting the loop (issue #175). Track
    # the task in ``_critical_listener_tasks`` so the readiness probe flips
    # red if it ever finishes (issue #192, mirrored from immune).
    _critical_listener_tasks["turn_request"] = asyncio.create_task(
        subscribe_supervised(
            nc,
            CORTEX_TURN_REQUEST,
            _handle_turn_message,
            queue="maki-cortex",
            name="cortex.turn_request",
        ),
        name="cortex.turn_request_listener",
    )
    log.info("Supervised turn-request listener started", extra={"subject": CORTEX_TURN_REQUEST})

    # Keep main() alive — supervised listener runs in the task above.
    await asyncio.Event().wait()


def cli():
    asyncio.run(main())
