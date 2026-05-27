"""maki-recall: Memory service backed by Mem0.

Provides REST API for memory storage, search, and retrieval
using pgvector + Neo4j graph store + Ollama embeddings.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import neo4j
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from maki_common import configure_logging
from mem0 import Memory
from pydantic import BaseModel, Field

configure_logging()
log = logging.getLogger(__name__)


# Background init/retry tunables. Conservative — the dependency outages we've
# seen typically clear in seconds to minutes, so we want to be ready quickly
# once they do without hammering pgvector/embedder/neo4j while they're down.
INIT_RETRY_SECONDS = 5
INIT_RETRY_MAX_SECONDS = 60
GRAPH_RETRY_INTERVAL_SECONDS = 5
GRAPH_RETRY_MAX_SECONDS = 300  # cap graph backoff at 5 minutes — see #262

# Number of failed init attempts after which we promote the per-failure log
# from WARNING to ERROR so cluster paging dashboards (typically ERROR+) catch
# a recall that's been stuck longer than a transient blip. Picked so a normal
# multi-minute dependency hiccup stays at WARNING but anything pathological
# (#258 saw 79h of WARNING-only logging) flips loud. See #258.
INIT_ERROR_LOG_AFTER_ATTEMPTS = 5

# Once stuck this long, /health body marks the init as "stuck" so immune (and
# any human reading /health directly) can distinguish "warming up normally"
# from "wedged on the same dependency for ages". See #258.
INIT_STUCK_THRESHOLD_S = 300  # 5 minutes

# Graph retry equivalent of INIT_ERROR_LOG_AFTER_ATTEMPTS. Same rationale —
# silent log.debug retries left a graph-disabled "ok" state running for days
# in #262. Default a touch higher than vector init because graph is non-fatal.
GRAPH_ERROR_LOG_AFTER_ATTEMPTS = 5

# Once graph init has been failing for this long, /health flips from ok to
# degraded so immune notices the component is running but missing half its
# brain. See #262 — graph_enabled=False stayed silent because graph is
# optional, but in practice every cortex turn loses graph context.
GRAPH_DEGRADED_AFTER_S = 600  # 10 minutes


def _build_pg_uri() -> str:
    user = quote_plus(os.environ.get("POSTGRES_USER", "maki"))
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    hosts = os.environ.get("POSTGRES_HOST", "maki-vault")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "maki")
    host_port = ",".join(f"{h}:{port}" for h in hosts.split(","))
    return f"postgresql://{user}:{password}@{host_port}/{db}?target_session_attrs=read-write"


def _build_config() -> tuple[dict[str, Any], bool]:
    """Build mem0 config from env. Returns (config, neo4j_requested).

    Pure config construction — no network I/O. Safe to call from lifespan
    and from retry loops without side effects.
    """
    config: dict[str, Any] = {
        "version": "v1.1",
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "collection_name": os.environ.get("POSTGRES_COLLECTION_NAME", "memories"),
                "embedding_model_dims": int(os.environ.get("EMBEDDING_DIMS", "768")),
                "connection_string": _build_pg_uri(),
            },
        },
        "llm": {
            "provider": os.environ.get("LLM_PROVIDER", "openai"),
            "config": {
                "model": os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514"),
                "temperature": 0,
                "max_tokens": 2000,
                "openai_base_url": os.environ.get("LLM_URL", "http://maki-synapse:8080/v1"),
                "api_key": "dummy",
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": os.environ.get("EMBEDDER_MODEL", "nomic-embed-text"),
                "ollama_base_url": os.environ.get("OLLAMA_URL", "http://maki-embed:11434"),
            },
        },
        "history_db_path": os.environ.get("HISTORY_DB_PATH", "/data/history.db"),
    }

    neo4j_uri = os.environ.get("NEO4J_URI", "")
    neo4j_requested = bool(neo4j_uri)
    if neo4j_uri:
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": neo4j_uri,
                "username": os.environ.get("NEO4J_USERNAME", "neo4j"),
                "password": os.environ.get("NEO4J_PASSWORD", ""),
            },
        }
    return config, neo4j_requested


# Module-level state initialised by the lifespan. `memory` stays None until
# Mem0 has been successfully built; until then /health returns 503 and all
# data endpoints return 503 instead of NPE'ing on a None attribute. See #135.
memory: Memory | None = None
graph_enabled: bool = False
_init_task: asyncio.Task[None] | None = None
_graph_retry_task: asyncio.Task[None] | None = None

# Long-lived Neo4j driver dedicated to the /health probe. neo4j drivers are
# designed to be created once per application and shared; opening a fresh
# driver per probe (every ~10s) burns sockets and DNS lookups. See #176.
_neo4j_probe_driver: neo4j.Driver | None = None


# Structured init telemetry, surfaced in /health body so a stuck-on-init pod
# is diagnosable without pod logs. See #258 — three documented episodes of
# silent multi-day hangs because the loop only emitted a WARNING per failure
# with no cumulative state, init duration, or last-error type recorded.
_init_state: dict[str, Any] = {
    "attempts": 0,
    "started_at": 0.0,  # wall-clock when init loop first entered, 0 until start
    "last_error": None,  # "Type: message" or None on success
    "last_error_at": 0.0,
    "last_attempt_duration_s": 0.0,  # how long the most recent attempt took
    "last_attempt_phase": None,  # "vector" | "graph" | None — which Memory.from_config call failed
    "ready_at": 0.0,  # wall-clock when init completed; 0 until ready
}


# Same shape as _init_state but for the background graph-recovery retry loop.
# Surfaced in /health so the silent #262 failure mode (debug-level logging,
# graph stays false for hours, status still reads ok) becomes observable.
_graph_state: dict[str, Any] = {
    "requested": False,
    "attempts": 0,
    "started_at": 0.0,
    "last_error": None,
    "last_error_at": 0.0,
    "last_attempt_duration_s": 0.0,
    "recovered_at": 0.0,
}


def _format_exception(exc: BaseException) -> str:
    """Format an exception for structured logging / /health body.

    ``str(exc)`` alone flattens the type — useful to keep `httpx.ConnectError:
    [Errno 111]` distinct from `psycopg.OperationalError: connection refused`.
    Length-capped so a multi-line traceback doesn't blow up /health responses.
    """
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > 500:
        text = text[:497] + "..."
    return text


async def _init_mem0() -> None:
    """Initialise Mem0 in the background, retrying on failure.

    Two-phase init so graph-store failures stay localised (#198):
      1. Build a vector-only Memory first. Failure here means a foundational
         backend is down (pgvector, embedder, LLM); we retry with backoff and
         surface the actual exception verbatim — the historical bug was to
         catch the failure of a graph-enabled init and misattribute *any*
         failure (pgvector, embedder, ...) to the graph store.
      2. If Neo4j was requested, attempt to upgrade to a graph-enabled Memory.
         Failure here is the only case where the "graph store unreachable"
         warning is correct; we keep the vector-only instance and schedule
         ``_retry_graph_init`` to recover the graph in the background.

    On success, sets the module globals ``memory`` and ``graph_enabled``.

    Telemetry: each attempt updates ``_init_state`` (attempts, last_error,
    last_attempt_duration_s, last_attempt_phase) so /health and any external
    observer can distinguish "warming up" from "wedged on the same error for
    hours". After ``INIT_ERROR_LOG_AFTER_ATTEMPTS`` failed attempts the per-
    failure log is promoted from WARNING to ERROR so cluster paging dashboards
    (typically ERROR+) finally see it. See #258.
    """
    global memory, graph_enabled, _graph_retry_task
    backoff = INIT_RETRY_SECONDS
    _init_state["started_at"] = time.time()
    while True:
        config, neo4j_requested = _build_config()
        _graph_state["requested"] = neo4j_requested
        _init_state["attempts"] += 1
        attempt = _init_state["attempts"]
        log.info(
            "Initializing Mem0",
            extra={
                "vector_store": "pgvector",
                "graph_store": "neo4j" if neo4j_requested else "disabled",
                "llm_provider": config["llm"]["provider"],
                "llm_model": config["llm"]["config"]["model"],
                "embedder_model": config["embedder"]["config"]["model"],
                "attempt": attempt,
            },
        )

        vector_only_config = {k: v for k, v in config.items() if k != "graph_store"}
        attempt_started = time.time()
        try:
            vector_mem = await asyncio.to_thread(Memory.from_config, vector_only_config)
        except Exception as vec_err:
            # Foundational backend down (pgvector / embedder / LLM). Do NOT
            # blame the graph store — that misattribution was #198.
            duration = time.time() - attempt_started
            err_text = _format_exception(vec_err)
            _init_state["last_error"] = err_text
            _init_state["last_error_at"] = time.time()
            _init_state["last_attempt_duration_s"] = round(duration, 3)
            _init_state["last_attempt_phase"] = "vector"
            stuck_for = time.time() - _init_state["started_at"]
            # Escalate log level once we've been failing long enough that a
            # paging dashboard should see it. First few failures stay WARNING
            # so a normal 30s-during-rollout outage doesn't page.
            log_fn = log.error if attempt >= INIT_ERROR_LOG_AFTER_ATTEMPTS else log.warning
            log_fn(
                "Mem0 vector-store init failed",
                extra={
                    "error": err_text,
                    "attempt": attempt,
                    "attempt_duration_s": round(duration, 3),
                    "stuck_for_s": round(stuck_for, 1),
                    "next_retry_in_s": backoff,
                    "phase": "vector",
                },
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, INIT_RETRY_MAX_SECONDS)
            continue

        if not neo4j_requested:
            memory = vector_mem
            graph_enabled = False
            _init_state["last_error"] = None
            _init_state["last_attempt_duration_s"] = round(time.time() - attempt_started, 3)
            _init_state["last_attempt_phase"] = "vector"
            _init_state["ready_at"] = time.time()
            log.info(
                "Mem0 ready",
                extra={
                    "graph_enabled": False,
                    "attempts_to_ready": attempt,
                    "init_duration_s": round(time.time() - _init_state["started_at"], 1),
                },
            )
            return

        # Vector-only is up; attempt the graph upgrade. Failure here is
        # genuinely graph-related — the warning text is finally accurate.
        graph_started = time.time()
        try:
            graph_mem = await asyncio.to_thread(Memory.from_config, config)
            memory = graph_mem
            graph_enabled = True
            _init_state["last_error"] = None
            _init_state["last_attempt_duration_s"] = round(time.time() - attempt_started, 3)
            _init_state["last_attempt_phase"] = "graph"
            _init_state["ready_at"] = time.time()
            log.info(
                "Mem0 ready",
                extra={
                    "graph_enabled": True,
                    "attempts_to_ready": attempt,
                    "init_duration_s": round(time.time() - _init_state["started_at"], 1),
                },
            )
            return
        except Exception as graph_err:
            err_text = _format_exception(graph_err)
            _init_state["last_error"] = err_text
            _init_state["last_error_at"] = time.time()
            _init_state["last_attempt_duration_s"] = round(time.time() - graph_started, 3)
            _init_state["last_attempt_phase"] = "graph"
            log.warning(
                "Mem0 graph store unreachable — running vector-only; will retry graph init in background",
                extra={
                    "error": err_text,
                    "graph_attempt_duration_s": round(time.time() - graph_started, 3),
                },
            )
            memory = vector_mem
            graph_enabled = False
            _init_state["ready_at"] = time.time()
            if _graph_retry_task is None or _graph_retry_task.done():
                _graph_retry_task = asyncio.create_task(_retry_graph_init(), name="recall.graph_retry")
            return


async def _retry_graph_init() -> None:
    """Periodically attempt to upgrade vector-only Mem0 to graph-enabled.

    Runs only while ``graph_enabled`` is False but the env still asks for
    Neo4j. On success it atomically swaps in a new graph-enabled Memory
    instance so subsequent writes/searches include the graph store.

    Fixed in #262: previously used a fixed 60s sleep and ``log.debug`` per
    failure, which meant a Neo4j outage produced no visible signal at all —
    /health stayed "ok" with ``graph: false`` and operators had no idea
    cortex was losing graph context for hours. Now uses exponential backoff
    capped at ``GRAPH_RETRY_MAX_SECONDS``, promotes the first failure per
    streak to WARNING, and surfaces structured state in ``_graph_state`` for
    /health to expose.
    """
    global memory, graph_enabled
    _graph_state["started_at"] = time.time()
    backoff = GRAPH_RETRY_INTERVAL_SECONDS
    while not graph_enabled:
        await asyncio.sleep(backoff)
        config, neo4j_requested = _build_config()
        _graph_state["requested"] = neo4j_requested
        if not neo4j_requested:
            # Neo4j was removed from config — no graph to recover.
            log.info("Graph retry exiting: NEO4J_URI no longer set")
            return
        _graph_state["attempts"] += 1
        attempt = _graph_state["attempts"]
        attempt_started = time.time()
        try:
            mem = await asyncio.to_thread(Memory.from_config, config)
            memory = mem
            graph_enabled = True
            _graph_state["last_error"] = None
            _graph_state["last_attempt_duration_s"] = round(time.time() - attempt_started, 3)
            _graph_state["recovered_at"] = time.time()
            log.info(
                "Mem0 graph store recovered",
                extra={
                    "graph_enabled": True,
                    "attempts_to_recover": attempt,
                    "recover_duration_s": round(time.time() - _graph_state["started_at"], 1),
                },
            )
            return
        except Exception as e:
            duration = time.time() - attempt_started
            err_text = _format_exception(e)
            _graph_state["last_error"] = err_text
            _graph_state["last_error_at"] = time.time()
            _graph_state["last_attempt_duration_s"] = round(duration, 3)
            stuck_for = time.time() - _graph_state["started_at"]
            # First attempt per streak (or every Nth) gets WARNING — beyond
            # that drop back to DEBUG to avoid spamming WARN every retry.
            # After many attempts, escalate to ERROR so paging picks it up.
            if attempt >= GRAPH_ERROR_LOG_AFTER_ATTEMPTS:
                log_fn = log.error
            elif attempt == 1 or attempt % 5 == 0:
                log_fn = log.warning
            else:
                log_fn = log.debug
            log_fn(
                "Mem0 graph store still unreachable",
                extra={
                    "error": err_text,
                    "attempt": attempt,
                    "attempt_duration_s": round(duration, 3),
                    "stuck_for_s": round(stuck_for, 1),
                    "next_retry_in_s": backoff,
                },
            )
            backoff = min(backoff * 2, GRAPH_RETRY_MAX_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spin up Mem0 initialization as a background task on startup.

    Init runs in the background so the FastAPI app registers immediately
    and /health can return 503 "initializing" while the underlying stores
    come up. See issue #135 — the previous module-import-time init caused
    k8s crashloops with no readiness signal whenever pgvector / embedder /
    Neo4j was briefly unreachable at boot.
    """
    global _init_task, _neo4j_probe_driver
    # Log the actual HTTP endpoint inventory so a probe/image mismatch is
    # diagnosable from `kubectl logs` alone. #263's root-cause hypothesis
    # was specifically a manifest-pointed-at-missing-endpoint scenario;
    # surfacing the live route table catches that class of bug instantly
    # ("manifest says livenessProbe=/livez but image only exposes /live").
    routes = sorted(
        f"{','.join(sorted(r.methods))} {r.path}"
        for r in app.routes
        if hasattr(r, "methods") and hasattr(r, "path") and r.methods
    )
    log.info(
        "maki-recall starting; scheduling Mem0 init",
        extra={"http_routes": routes, "route_count": len(routes)},
    )
    _init_task = asyncio.create_task(_init_mem0(), name="recall.mem0_init")
    try:
        yield
    finally:
        for task in (_init_task, _graph_retry_task):
            if task is not None and not task.done():
                task.cancel()
        if _neo4j_probe_driver is not None:
            try:
                _neo4j_probe_driver.close()
            except Exception:
                pass
            _neo4j_probe_driver = None


app = FastAPI(title="maki-recall", version="0.0.1", lifespan=lifespan)


class MemoryCreate(BaseModel):
    messages: list[dict[str, str]] = Field(..., description="List of {role, content} messages.")
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    query: str
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    limit: int | None = None


def _require_memory() -> Memory:
    """Return the live Mem0 instance or raise 503 if init hasn't completed.

    Centralised so every data endpoint fails the same way during boot or
    while a dependency is recovering.
    """
    if memory is None:
        raise HTTPException(status_code=503, detail="Mem0 initializing")
    return memory


def _probe_pgvector() -> str | None:
    """Cheapest possible pgvector reachability check.

    Returns None on success, an error string on failure. Opens a fresh
    short-lived ``psycopg.connect`` rather than reaching into the mem0
    PGVector backend's private ``_get_cursor`` API (#176): a mem0 minor
    bump rename would otherwise silently turn /health red. Trade-off: this
    no longer exercises mem0's connection pool, so a pool-exhaustion-only
    failure won't show up here — but the data endpoints fail loud on the
    next real request, and a stable public probe is preferable to a
    private-API tripwire that breaks on dependency upgrades.
    """
    try:
        with psycopg.connect(_build_pg_uri(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def _probe_neo4j() -> str | None:
    """Cheapest possible Neo4j reachability check.

    Returns None on success, an error string on failure. Uses a long-lived
    dedicated probe driver (created lazily) rather than the mem0/langchain
    private chain ``mem.graph.graph.query`` (#176). A neo4j minor bump
    that renames langchain's internal attributes would otherwise wedge our
    liveness probe even though the database is fine.
    """
    global _neo4j_probe_driver
    uri = os.environ.get("NEO4J_URI", "")
    if not uri:
        return None
    try:
        if _neo4j_probe_driver is None:
            _neo4j_probe_driver = neo4j.GraphDatabase.driver(
                uri,
                auth=(
                    os.environ.get("NEO4J_USERNAME", "neo4j"),
                    os.environ.get("NEO4J_PASSWORD", ""),
                ),
                connection_timeout=3,
            )
        with _neo4j_probe_driver.session() as session:
            session.run("RETURN 1 AS ok").consume()
    except Exception as e:
        # Drop the driver on failure so a transient outage doesn't poison
        # the cached connection for the rest of the pod's life.
        if _neo4j_probe_driver is not None:
            try:
                _neo4j_probe_driver.close()
            except Exception:
                pass
            _neo4j_probe_driver = None
        return f"{type(e).__name__}: {e}"
    return None


def _init_snapshot() -> dict[str, Any]:
    """Snapshot of init telemetry for /health body.

    Pure read of module state; safe to call from any handler. The point of
    this snapshot is that #258's silent-hang mode can be diagnosed from a
    single ``curl /health`` — last_init_error tells you which dependency,
    init_duration_s tells you for how long.
    """
    now = time.time()
    started = _init_state["started_at"] or 0.0
    init_duration_s = round(now - started, 1) if started else 0.0
    snap: dict[str, Any] = {
        "init_attempts": _init_state["attempts"],
        "init_duration_s": init_duration_s,
        "last_init_error": _init_state["last_error"],
        "last_init_phase": _init_state["last_attempt_phase"],
        "last_attempt_duration_s": _init_state["last_attempt_duration_s"],
    }
    if _init_state["last_error"] is not None and _init_state["last_error_at"]:
        snap["last_error_age_s"] = round(now - _init_state["last_error_at"], 1)
    if memory is None and init_duration_s >= INIT_STUCK_THRESHOLD_S:
        snap["init_stuck"] = True
    return snap


def _graph_snapshot() -> dict[str, Any]:
    """Snapshot of graph-retry telemetry for /health body. See #262."""
    now = time.time()
    started = _graph_state["started_at"] or 0.0
    retry_duration_s = round(now - started, 1) if started else 0.0
    snap: dict[str, Any] = {
        "graph_requested": _graph_state["requested"],
        "graph_attempts": _graph_state["attempts"],
        "graph_retry_duration_s": retry_duration_s,
        "last_graph_error": _graph_state["last_error"],
        "last_graph_attempt_duration_s": _graph_state["last_attempt_duration_s"],
    }
    if _graph_state["last_error"] is not None and _graph_state["last_error_at"]:
        snap["last_graph_error_age_s"] = round(now - _graph_state["last_error_at"], 1)
    return snap


@app.get("/live")
def live():
    """Liveness probe — process-only health check.

    Returns 200 as long as the FastAPI event loop is responsive. Does NOT
    touch Mem0, pgvector, Neo4j, the embedder, or the LLM. Used by k8s
    ``livenessProbe`` so that slow dependency boots (Mem0's synchronous
    ``Memory.from_config`` blocking on pgvector / neo4j / synapse) cannot
    cause the kubelet to restart the pod mid-init.

    Separated from ``/health`` per #253: previously both probes shared
    ``/health``, which returns 503 during Mem0 init, so a slow init would
    blow past the liveness budget and crashloop the pod just as it was
    about to come up. ``/health`` remains the readiness/startup signal.
    """
    return {"status": "alive"}


@app.get("/health")
def health():
    """Readiness/startup probe.

    Returns 503 with ``status: initializing`` while Mem0 hasn't finished
    booting (so k8s and maki-immune wait instead of crashlooping blind),
    200 when the stores are usable, and 503 ``status: degraded`` when one
    of the backing stores fails its probe. See issues #135, #169, #253.

    Init/graph telemetry (#258, #262): the 503 init body and the 200
    body both include the structured snapshots so a curl /health tells
    you how many attempts have happened, how long the last one took,
    the last error, and whether the pod is stuck. That's the diagnostic
    surface the silent-hang incidents (#251, #257, #258, #259) needed.

    Used by ``readinessProbe`` and ``startupProbe``. The ``livenessProbe``
    points at ``/live`` instead so dependency slowness doesn't kill the
    pod mid-init.
    """
    mem = memory
    if mem is None:
        body: dict[str, Any] = {"status": "initializing", "graph": False}
        body.update(_init_snapshot())
        return JSONResponse(status_code=503, content=body)

    checks: dict[str, Any] = {"status": "ok", "graph": graph_enabled}
    checks.update(_init_snapshot())

    pg_err = _probe_pgvector()
    if pg_err is not None:
        log.warning("recall /health pg probe failed: %s", pg_err)
        checks["status"] = "degraded"
        checks["pg"] = "down"

    if graph_enabled:
        neo_err = _probe_neo4j()
        if neo_err is not None:
            log.warning("recall /health neo4j probe failed: %s", neo_err)
            checks["status"] = "degraded"
            checks["neo4j"] = "down"

    # Graph-retry surface (#262). Always include for observability; flip to
    # degraded only when the retry has been failing past the threshold so a
    # brief Neo4j hiccup during boot doesn't trip immune.
    if _graph_state["requested"]:
        checks.update(_graph_snapshot())
        if not graph_enabled:
            started = _graph_state["started_at"] or 0.0
            stuck_for = time.time() - started if started else 0.0
            if stuck_for >= GRAPH_DEGRADED_AFTER_S:
                checks["status"] = "degraded"
                checks["graph_stuck"] = True

    if checks["status"] != "ok":
        return JSONResponse(status_code=503, content=checks)
    return checks


@app.post("/memories")
def add_memory(req: MemoryCreate):
    mem = _require_memory()
    if not any([req.user_id, req.agent_id, req.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")
    params = {k: v for k, v in req.model_dump().items() if v is not None and k != "messages"}
    try:
        return JSONResponse(content=mem.add(messages=req.messages, **params))
    except Exception as e:
        log.exception("Error adding memory")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/memories")
def get_memories(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    mem = _require_memory()
    if not any([user_id, agent_id, run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")
    params = {k: v for k, v in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if v is not None}
    try:
        return mem.get_all(**params)
    except Exception as e:
        log.exception("Error getting memories")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search")
def search_memories(req: SearchRequest):
    mem = _require_memory()
    # mem0 requires entity identifiers (user_id, agent_id, run_id) inside filters={},
    # not as top-level kwargs — only limit stays top-level.
    identity_keys = {"user_id", "agent_id", "run_id"}
    filters = {k: v for k, v in req.model_dump().items() if v is not None and k in identity_keys}
    kwargs: dict[str, Any] = {}
    if filters:
        kwargs["filters"] = filters
    if req.limit is not None:
        kwargs["limit"] = req.limit
    try:
        return mem.search(query=req.query, **kwargs)
    except Exception as e:
        log.exception("Error searching memories")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    mem = _require_memory()
    try:
        mem.delete(memory_id=memory_id)
        return {"message": "Memory deleted"}
    except Exception as e:
        log.exception("Error deleting memory")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/memories")
def delete_all_memories(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    mem = _require_memory()
    if not any([user_id, agent_id, run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")
    params = {k: v for k, v in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if v is not None}
    try:
        mem.delete_all(**params)
        return {"message": "All memories deleted"}
    except Exception as e:
        log.exception("Error deleting memories")
        raise HTTPException(status_code=500, detail=str(e))


def cli():
    import uvicorn

    uvicorn.run("maki_recall.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
