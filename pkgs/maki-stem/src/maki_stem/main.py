"""maki-stem: Brainstem — The Coordinator.

Manages context, publishes turn requests to cortex, collects responses.
Idle heartbeat loop, self-awareness, Discord relay, conversation history, memory.

This module owns process lifecycle only: NATS/JetStream/KV/Postgres bootstrap
in :func:`lifespan`, assembly of :class:`StemContext`, spawning of every
supervised listener, and the FastAPI HTTP surface (``/health``, ``/turn``).
All feature logic lives in single-responsibility submodules (``conversation``,
``memory``, ``system_state``, ``cortex_io``, ``discord_handler``,
``loop_runner``, ``db_listeners``, ``time_windows``, ``github_setup``,
``identity``) — see #134 for the split.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from maki_common import (
    PendingQueues,
    build_pg_dsn,
    configure_logging,
    connect_nats,
    init_kv,
    spawn_background,
    subscribe_supervised,
)
from maki_common.subjects import CONFIG_SYNC
from pydantic import BaseModel

from maki_stem.conversation import (
    conversation_sync_listener,
    get_recent_conversation,
    history_size,
    init_conversation_stream,
)
from maki_stem.cortex_io import (
    TURN_TIMEOUT,
    active_turns,
    cortex_heartbeat_watcher,
    process_turn,
    response_listener,
)
from maki_stem.db_listeners import (
    db_query_listener,
    pattern_query_listener,
    pattern_update_listener,
    pattern_write_listener,
)
from maki_stem.discord_handler import ears_listener
from maki_stem.github_setup import init_github_client
from maki_stem.identity import DEFAULT_IDENTITY, seed_identity
from maki_stem.loop_runner import discover_loops
from maki_stem.loops import StemContext, _run_loop
from maki_stem.memory import feed_memories, memory_store_listener, search_memories
from maki_stem.system_state import format_system_state, gather_system_state
from maki_stem.time_windows import in_quiet_hours, in_work_hours
from maki_stem.trading import (
    trading_manual_listener,
    trading_signal_listener,
    trading_tool_listener,
)

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")

# Single source of truth for the NATS queue group name shared across all stem
# pods. Every write-side or request/reply listener MUST subscribe with this
# queue so that a rolling deploy (where two pods coexist briefly) doesn't
# cause duplicate writes or duplicate request handling. Broadcast listeners
# (per-pod state, fan-out tool dispatch) intentionally omit it — see the
# comment at each subscribe site.
STEM_QUEUE = "maki-stem"

CONFIG_BUCKET = "maki-cortex-config"
LOCK_BUCKET = "maki-lock"

INSTANCE_ID = f"stem-{uuid.uuid4().hex[:8]}"

# Component health probes used by ``system_state.gather_system_state`` when
# immune is unreachable. Keep in sync with the deployed service DNS names.
HEALTH_ENDPOINTS = {
    "recall": os.environ.get("RECALL_URL", "http://maki-recall:8000"),
    "synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
    "cortex": os.environ.get("CORTEX_URL", "http://maki-cortex:8080"),
}

# Idle loop frequency is controlled by IDLE_CRON + the distributed TTL lock in idle.py.
# No per-loop "max per day" counter — that's redundant, not distributed, and a foot-gun.
DEFAULT_CORTEX_CONFIG = {
    "chat_model": "",  # empty = cortex default; set to e.g. "claude-opus-4-6" to override
}

# ---- Global state ----------------------------------------------------------
_nc = None
_kv = None
_js = None
_config_kv = None
_lock_kv = None
_pending = PendingQueues()
_github = None  # GitHubIssueClient, initialized in lifespan if creds available
# Critical listener tasks. They're wrapped in ``subscribe_supervised`` so they
# should never exit cleanly on their own — if any is ``done()`` the readiness
# probe must flip red so kubelet restarts the pod (issue #175 / #192).
_critical_listener_tasks: dict[str, asyncio.Task] = {}
_loop_specs: list = []  # discovered LoopSpecs, populated in lifespan
_stem_ctx: StemContext | None = None
_trading_tool_registry: dict = {}  # name → async handler, populated per trading run
_permanent_trading_tools: dict = {}  # name → async handler, always-on read-only KV
_db_pool: asyncpg.Pool | None = None  # asyncpg connection pool, initialized in lifespan


async def _handle_config_sync(msg) -> None:
    """Apply one config update from a peer site."""
    try:
        data = json.loads(msg.data.decode())
        key = data.get("key", "")
        value = data.get("value", "")
        if key and _config_kv is not None:
            await _config_kv.put(key, value.encode())
            log.info("Config synced from peer", extra={"key": key, "value": value})
    except Exception:
        log.exception("Config sync error")


async def _config_sync_listener():
    """Apply config updates broadcast from other sites.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently leaving this pod's KV cache stale
    (issue #175). Broadcast: each pod must apply the update locally — no
    queue group.
    """
    await subscribe_supervised(
        _nc,
        CONFIG_SYNC,
        _handle_config_sync,
        name="stem.config_sync",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _nc, _js, _kv, _config_kv, _lock_kv, _github, _db_pool, _stem_ctx, _loop_specs
    log.info("maki-stem starting", extra={"nats_url": NATS_URL, "instance_id": INSTANCE_ID})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    _kv = await seed_identity(_js)
    await init_conversation_stream(_js)

    _config_kv = await init_kv(_js, CONFIG_BUCKET)
    _lock_kv = await init_kv(_js, LOCK_BUCKET)

    _db_pool = await asyncpg.create_pool(dsn=build_pg_dsn(), min_size=1, max_size=3)
    log.info("PostgreSQL pool created")

    from maki_common.tools.trading_portfolio import make_trading_portfolio_tools

    _permanent_trading_tools.update(make_trading_portfolio_tools(_lock_kv))
    log.info("Permanent trading tools registered", extra={"tools": list(_permanent_trading_tools)})

    _github = init_github_client()

    # Assemble StemContext once — loops and the interactive turn path both
    # share it. Callables are wired to the extracted modules; the identity/
    # system-state summary helpers not on StemContext are imported directly
    # by ``cortex_io.process_turn``.
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
        search_memories=search_memories,
        feed_memories=feed_memories,
        gather_system_state=lambda: gather_system_state(
            _nc,
            conversation_history_size=history_size(),
            health_endpoints=HEALTH_ENDPOINTS,
        ),
        format_system_state=format_system_state,
        get_recent_conversation=get_recent_conversation,
        in_quiet_hours=in_quiet_hours,
        in_work_hours=in_work_hours,
    )
    _stem_ctx = ctx

    # Track every supervised listener so the readiness probe can fail if any
    # of them dies — see ``_critical_listener_tasks`` and #175/#192.
    _critical_listener_tasks["response"] = asyncio.create_task(
        response_listener(_nc, _pending), name="stem.response_listener"
    )
    _critical_listener_tasks["cortex_heartbeat"] = asyncio.create_task(
        cortex_heartbeat_watcher(_nc, _pending), name="stem.cortex_heartbeat_watcher"
    )
    _critical_listener_tasks["conversation_sync"] = asyncio.create_task(
        conversation_sync_listener(_nc, _js, INSTANCE_ID), name="stem.conversation_sync_listener"
    )
    _critical_listener_tasks["ears_in"] = asyncio.create_task(
        ears_listener(
            ctx,
            _loop_specs,  # populated below before ears actually dispatches — closure captures the list
            default_identity=DEFAULT_IDENTITY,
            health_endpoints=HEALTH_ENDPOINTS,
            conversation_history_size_fn=history_size,
            queue=STEM_QUEUE,
        ),
        name="stem.ears_listener",
    )
    _critical_listener_tasks["memory_store"] = asyncio.create_task(
        memory_store_listener(_nc, queue=STEM_QUEUE), name="stem.memory_store_listener"
    )
    _critical_listener_tasks["config_sync"] = asyncio.create_task(
        _config_sync_listener(), name="stem.config_sync_listener"
    )
    _critical_listener_tasks["db_query"] = asyncio.create_task(
        db_query_listener(_nc, _db_pool, queue=STEM_QUEUE), name="stem.db_query_listener"
    )
    _critical_listener_tasks["pattern_query"] = asyncio.create_task(
        pattern_query_listener(_nc, _db_pool, queue=STEM_QUEUE), name="stem.pattern_query_listener"
    )
    _critical_listener_tasks["pattern_update"] = asyncio.create_task(
        pattern_update_listener(_nc, _db_pool, queue=STEM_QUEUE), name="stem.pattern_update_listener"
    )
    _critical_listener_tasks["pattern_write"] = asyncio.create_task(
        pattern_write_listener(_nc, _db_pool, queue=STEM_QUEUE), name="stem.pattern_write_listener"
    )
    _critical_listener_tasks["trading_signal"] = asyncio.create_task(
        trading_signal_listener(_nc, _db_pool), name="stem.trading_signal_listener"
    )
    _critical_listener_tasks["trading_manual"] = asyncio.create_task(
        trading_manual_listener(_nc, _lock_kv), name="stem.trading_manual_listener"
    )
    _critical_listener_tasks["trading_tool"] = asyncio.create_task(
        trading_tool_listener(_nc, _trading_tool_registry, _permanent_trading_tools),
        name="stem.trading_tool_listener",
    )

    # Discover and start all loops (builtin + external via entry points).
    # ``_loop_specs`` is the list the ears listener closes over above — we
    # mutate in place so that closure sees the discovered specs without
    # having to re-spawn the listener.
    loops = discover_loops()
    _loop_specs.extend(loops)
    for spec in loops:
        spawn_background(_run_loop(spec, ctx), name=f"stem.loop.{spec.name}")

    yield

    if _db_pool:
        await _db_pool.close()
    await _nc.close()


app = FastAPI(title="maki-stem", version="0.0.1", lifespan=lifespan)


class TurnRequest(BaseModel):
    message: str


@app.get("/health")
def health():
    if not _nc or not _nc.is_connected:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "reason": "NATS not connected"})

    # Critical listeners are wrapped in ``subscribe_supervised`` and should
    # run forever. If any has exited, fail readiness so kubelet restarts
    # the pod (issue #175 / #192).
    dead_tasks = []
    for label, task in _critical_listener_tasks.items():
        if task is None or task.done():
            dead_tasks.append(label)
    if dead_tasks:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "Listener task(s) exited", "dead": dead_tasks},
        )

    now = time.time()
    for turn_id, started in active_turns.items():
        if now - started > TURN_TIMEOUT:
            return JSONResponse(
                status_code=503,
                content={"status": "stuck", "turn_id": turn_id, "running_seconds": int(now - started)},
            )
    return {"status": "ok", "active_turns": len(active_turns)}


@app.post("/turn")
async def turn(req: TurnRequest):
    if not _nc or not _nc.is_connected or _stem_ctx is None:
        raise HTTPException(status_code=503, detail="NATS not connected")
    _, response = await process_turn(
        _stem_ctx,
        req.message,
        default_identity=DEFAULT_IDENTITY,
        health_endpoints=HEALTH_ENDPOINTS,
        conversation_history_size=history_size(),
    )
    return {"response": response}


def cli():
    import uvicorn

    uvicorn.run("maki_stem.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
