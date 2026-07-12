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
from typing import Any

from maki_common import (
    configure_logging,
    connect_nats,
    default_health_endpoints,
    format_graph_block,
    format_memories_block,
    format_system_state_lines,
    init_kv,
    spawn_background,
    subscribe_supervised,
)
from maki_common.claude import TokenUsage, invoke_claude, stream_claude
from maki_common.health import tcp_health_server
from maki_common.repo import SyncError, build_github_auth, hard_sync, init_repo
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
# cancel it from inside the cortex so the tracked turn state is cleared, slot
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
# Token-free clone URL — auth is injected per-invocation via
# ``git -c http.extraheader=...`` inside ``maki_common.repo`` (issue #347).
CLONE_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git"

# Bare-name registry (matches the tool-facing convention used elsewhere in
# cortex/stem). Ports + env-var overrides come from the shared table in
# ``maki_common.endpoints`` — see #137 for the drift this consolidates. Self
# entry is overridden to hit the local process directly so ``check_component``
# gets a fresh single-shot reading rather than a Service round-robin hop.
HEALTH_ENDPOINTS = {
    **default_health_endpoints(),
    "cortex": f"http://localhost:{HEALTH_PORT}",
}

# Unique per startup — lets stem detect cortex restarts
SESSION_ID = uuid.uuid4().hex[:12]

_semaphore = asyncio.Semaphore(1)

# Hoisted from main() so handle_turn_request can use it for auto-pull
_github_private_key: str | None = None
# GitHubAuth instance built once at startup — reused by the per-turn hard-sync
# so we don't reconstruct it (and re-import GitHubAuth) on every turn.
_github_auth: Any | None = None


class _TurnState:
    """Single source of truth for the in-flight turn.

    Replaces four module globals (``_active_turn``, ``_active_turn_mode``,
    ``_active_turn_started``, ``_active_task``) that raced across concurrent
    turn handlers — heartbeats lied, the liveness-escape timer cleared early
    when the wrong handler's ``finally`` fired, and preemption could target a
    task that wasn't the one actually running. State is mutated only by the
    handler that currently holds ``_turn_lock``, so heartbeat / liveness /
    preemption always read a snapshot that matches the actually-running turn.
    See issue #203.
    """

    __slots__ = ("turn_id", "mode", "started", "task")

    def __init__(self) -> None:
        self.turn_id: str | None = None
        self.mode: str | None = None
        self.started: float | None = None
        self.task: asyncio.Task | None = None

    def set_active(
        self,
        *,
        turn_id: str,
        mode: str,
        started: float,
        task: asyncio.Task | None,
    ) -> None:
        self.turn_id = turn_id
        self.mode = mode
        self.started = started
        self.task = task

    def clear(self) -> None:
        self.turn_id = None
        self.mode = None
        self.started = None
        self.task = None


# Active turn tracking — exposed via heartbeat for immune awareness.
_turn_state = _TurnState()

# Dispatcher-level serialization gate. Wrapping the body of handle_turn_request
# in this lock enforces the cortex's actual single-turn-at-a-time intent and
# naturally serializes git auto-pull on the shared /repo/maki working tree.
# Without it, the four pieces of turn state above raced across concurrent
# handlers — see issue #203. The inner Claude semaphore becomes redundant
# (only one handler runs at a time) but is harmless and left as a backstop.
_turn_lock = asyncio.Lock()

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


def _liveness_check() -> tuple[bool, str | None]:
    """Return (ok, reason) for the ``/live`` liveness probe.

    Liveness answers a single question: "would a restart fix this?" It fails
    only for conditions that require kubelet to SIGKILL and reschedule the
    pod — a crashed heartbeat, a dead critical listener, or an event loop
    wedged below the application layer (see issue #185). It deliberately
    does *not* fail on NATS disconnection or missing startup state, because
    those either fix themselves (reconnect) or belong to the readiness
    probe's "don't route traffic to me" side of the split (issue #373).
    """
    # A heartbeat task that started and then died is a restart-worthy fault.
    # A heartbeat that hasn't started yet is startup ordering — readiness'
    # concern, not liveness'.
    if _heartbeat_task is not None and _heartbeat_task.done():
        if _heartbeat_task.cancelled():
            return False, "Heartbeat task cancelled"
        exc = _heartbeat_task.exception()
        return False, f"Heartbeat task crashed: {exc!r}"

    # Critical listeners are wrapped in ``subscribe_supervised`` and should
    # run forever. If any has exited, kubelet must restart the pod (#175).
    for label, task in _critical_listener_tasks.items():
        if task.done():
            if task.cancelled():
                return False, f"{label} listener cancelled"
            exc = task.exception()
            return False, f"{label} listener crashed: {exc!r}"

    # Hard liveness escape: if a turn is wedged way past the soft watchdog
    # window, kubelet must restart us. This is the only way out of an
    # uncancellable native call or a wedged event loop. Snapshot both fields
    # together so a concurrent clear() between the two reads can't surface
    # a stale started timestamp paired with a None turn_id.
    started = _turn_state.started
    turn_id = _turn_state.turn_id
    if started is not None:
        running_s = time.time() - started
        liveness_threshold = CORTEX_LIVENESS_TURN_MULTIPLIER * CORTEX_MAX_TURN_SECONDS
        if running_s > liveness_threshold:
            return (
                False,
                f"Turn {turn_id} wedged for {running_s:.0f}s "
                f"(> {liveness_threshold:.0f}s = {CORTEX_LIVENESS_TURN_MULTIPLIER}x watchdog) — "
                f"requesting kubelet restart",
            )

    return True, None


def _readiness_check() -> tuple[bool, str | None]:
    """Return (ok, reason) for the ``/health`` readiness probe.

    Readiness answers "should I receive turn traffic right now?" It fails
    on anything liveness fails on (a broken pod isn't ready either) plus
    startup-ordering and connectivity conditions that don't warrant a
    restart: NATS reconnecting, listeners not yet subscribed. See #373.
    """
    if _nc_ref is None or not _nc_ref.is_connected:
        return False, "NATS not connected"
    if _heartbeat_task is None:
        return False, "Heartbeat task not started"
    if not _critical_listener_tasks:
        return False, "Turn-request listener not started"
    return _liveness_check()


# Retained under the legacy name for any external caller that imported
# ``_health_check`` directly. Semantically identical to readiness — the
# stricter of the two, matching the pre-split behaviour.
_health_check = _readiness_check


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
        state_lines = format_system_state_lines(system_state)
        if state_lines:
            parts.append("## Your system state\n" + "\n".join(state_lines))
    elif system_state_summary:
        parts.append(f"## System: {system_state_summary}")

    memories_block = format_memories_block(turn.get("memories", []))
    if memories_block:
        parts.append(memories_block)

    graph_block = format_graph_block(turn.get("graph_context", []))
    if graph_block:
        parts.append(graph_block)

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

    # Auto-pull latest code if requested by the loop.
    #
    # Abort-on-failure semantics: if ANY step of the hard sync fails (token
    # mint, remote set-url, fetch, reset, clean) we publish a `done=True,
    # cancelled=True, reason="cortex_auto_sync_failed"` signal and return.
    # We do NOT proceed against whatever was last fetched.
    #
    # The previous inline pipeline (`fetch` → `reset --hard` → `clean`) only
    # logged a warning on the failing step and kept going, which meant a
    # transient fetch failure still ran `reset --hard origin/main` against a
    # stale `origin/main`. Work turns then edited stale files, idle/care turns
    # reflected on stale code, and the only signal was one buried WARNING.
    # The "Auto-sync before turn" log line was unconditional too — it lied
    # whenever sync silently failed. See issue #290.
    if git_pull and os.path.exists(REPO_PATH):
        try:
            await hard_sync(REPO_PATH, github_auth=_github_auth, clone_url=CLONE_URL)
        except SyncError as exc:
            # stderr is pre-redacted inside hard_sync — safe to log and publish.
            log.error(
                "Auto-sync failed — aborting turn to avoid running against stale code",
                extra={
                    "turn_id": turn_id,
                    "mode": mode,
                    "step": exc.step,
                    "returncode": exc.returncode,
                    "stderr": (exc.stderr or "")[:500],
                },
            )
            done_msg = {
                "turn_id": turn_id,
                "response": "",
                "done": True,
                "cancelled": True,
                "reason": "cortex_auto_sync_failed",
                "error": f"git {exc.step} failed (rc={exc.returncode})",
            }
            try:
                await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
            except Exception:
                log.exception(
                    "Failed to publish auto-sync-failure done signal",
                    extra={"turn_id": turn_id},
                )
            return
        except Exception:
            # Anything not caught as SyncError (programming error in the
            # caller path, asyncio cancellation propagation, etc.) is also a
            # reason to abort — running with stale code is worse than failing
            # loudly.
            log.error(
                "Auto-sync raised unexpectedly — aborting turn",
                exc_info=True,
                extra={"turn_id": turn_id, "mode": mode},
            )
            done_msg = {
                "turn_id": turn_id,
                "response": "",
                "done": True,
                "cancelled": True,
                "reason": "cortex_auto_sync_failed",
            }
            try:
                await nc.publish(CORTEX_TURN_RESPONSE, json.dumps(done_msg).encode())
            except Exception:
                log.exception(
                    "Failed to publish auto-sync-failure done signal",
                    extra={"turn_id": turn_id},
                )
            return

        log.info("Auto-sync before turn", extra={"turn_id": turn_id})
        # Invalidate code graph cache — files on disk changed
        from maki_common.tools.codegraph_tools import invalidate_graph_cache

        invalidate_graph_cache(REPO_PATH)

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
    submitter is unblocked, and let ``finally`` clear the tracked turn state
    so the next turn can run. Heartbeat is in a separate task and stays
    healthy throughout — without this, only an external CORTEX_STUCK signal
    (from a still-waiting submitter) could ever recover the pod. See #150.

    The body is wrapped in ``_turn_lock`` so only one handler ever runs at a
    time — heartbeat / liveness / preemption read state set under the same
    lock and therefore always match the actually-running turn. See #203.
    """
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

    # Serialize at the dispatcher: only one handler body runs at a time.
    # Concurrent dispatches queue on this lock instead of overwriting each
    # other's turn-state. See issue #203.
    async with _turn_lock:
        # State is set under the lock so heartbeat / liveness / preemption
        # always observe a snapshot that matches the actually-running turn.
        # Record our own task so an interactive turn can preempt this one.
        _turn_state.set_active(
            turn_id=turn_id,
            mode=mode,
            started=time.time(),
            task=asyncio.current_task(),
        )

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
            _turn_state.clear()


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
                    "active_turn": _turn_state.turn_id,
                    "turn_mode": _turn_state.mode,
                    "turn_started": _turn_state.started,
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
    # the pod starts. ``/health`` (readiness) returns 503 until NATS, the
    # turn subscription and the heartbeat task are all live, keeping traffic
    # off during startup. ``/live`` (liveness) is the narrower "would a
    # restart fix this?" check — it stays green during NATS reconnects so
    # kubelet doesn't SIGKILL a pod that's just waiting for the nerve to
    # come back. Split enabled by issue #373.
    await tcp_health_server(
        port=HEALTH_PORT,
        checks={"/live": _liveness_check, "/health": _readiness_check},
    )
    log.info("Health server started", extra={"port": HEALTH_PORT})

    global _nc_ref, _heartbeat_task
    nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _nc_ref = nc
    js = nc.jetstream()
    config_kv = await init_kv(js, "maki-cortex-config")

    # Load GitHub App private key if configured
    global _github_private_key, _github_auth
    github_private_key = None
    if GITHUB_PRIVATE_KEY_PATH and os.path.exists(GITHUB_PRIVATE_KEY_PATH):
        with open(GITHUB_PRIVATE_KEY_PATH) as f:
            github_private_key = f.read()
        _github_private_key = github_private_key
        log.info("GitHub App private key loaded", extra={"path": GITHUB_PRIVATE_KEY_PATH})

    # Build the GitHubAuth instance once so both the startup clone/pull and the
    # per-turn hard-sync reuse it — no inline reconstruction on every turn, no
    # authed URL hand-assembly. The token itself is minted lazily (per git
    # invocation) inside ``maki_common.repo`` (issue #347).
    _github_auth = build_github_auth(GITHUB_APP_ID, github_private_key, GITHUB_INSTALLATION_ID)

    # Clone (fresh) or pull (existing) the repo for self-evolution tools. This
    # goes through ``maki_common.repo.init_repo`` — the same helper immune uses
    # — so cortex no longer reinvents clone/pull with a sync ``subprocess.run``
    # inside ``async def main()``, and the "repo already present" case actually
    # syncs to origin/main at startup instead of waiting for the first turn.
    # ``init_repo`` also configures git user.name/user.email on a fresh clone.
    if _github_auth or os.path.exists(REPO_PATH):
        ok = await init_repo(REPO_PATH, clone_url=CLONE_URL, github_auth=_github_auth)
        if not ok:
            log.warning("Repo init at startup failed — self-evolution tools may be degraded")

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

    spawn_background(heartbeat_loop(nc), name="cortex.heartbeat_loop")
    log.info("Heartbeat loop started")

    async def _handle_turn_message(msg) -> None:
        """Dispatch one CORTEX_TURN_REQUEST message into a background task.

        Kept narrow: preemption is the only decision made here. The spawned
        task acquires ``_turn_lock`` inside ``handle_turn_request`` so the
        cortex only ever runs one handler body at a time — concurrent
        dispatches queue safely on the lock instead of racing on the
        turn-state globals the way they used to (issue #203). The supervised
        loop owns the subscribe/iterate lifecycle so a silent stream drain
        re-subscribes instead of leaving cortex blind to new turns (#175).
        """
        try:
            turn = json.loads(msg.data.decode())
            mode = turn.get("mode", "")
            turn_id = turn.get("turn_id", "unknown")
        except Exception:
            mode = ""
            turn_id = "unknown"

        is_background = mode in _BACKGROUND_MODES

        # Preempt: an interactive turn cancels a currently-running background
        # turn. Snapshot the tracked task/mode — they're written only under
        # ``_turn_lock`` so reading them here gives a consistent view of the
        # actually-running handler. The cancellation propagates through
        # ``asyncio.wait_for`` → the handler's ``finally`` clears state and
        # releases the lock so this new turn can claim it.
        active_task = _turn_state.task
        active_mode = _turn_state.mode
        if not is_background and active_task is not None and active_mode in _BACKGROUND_MODES:
            active_task.cancel()
            log.info("Preempted background turn", extra={"cancelled_mode": active_mode})

        # No need to track the spawned task here — _turn_state.task gets
        # populated under _turn_lock the moment the handler enters its body.
        # ``spawn_background`` anchors against GC and logs uncaught exceptions
        # so a raised handler doesn't vanish silently (issue #123).
        spawn_background(handle_turn_request(msg, nc, mcp_server), name=f"cortex.turn.{turn_id}")

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
