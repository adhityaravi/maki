"""maki-recall: Memory service backed by Mem0.

Provides REST API for memory storage, search, and retrieval
using pgvector + Neo4j graph store + Ollama embeddings.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import neo4j
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from maki_common import build_pg_dsn, configure_logging, connect_nats
from maki_common.subjects import IMMUNE_ALERT
from mem0 import Memory
from pydantic import BaseModel, Field

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")


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

# Once vector init has been stuck this long, fire a NATS alert on the same
# subject immune uses for component escalations. Default 5 min — long enough
# that a normal boot doesn't page, short enough that a wedged init is loudly
# visible from #maki-general within minutes instead of after 79h. See #258.
INIT_NATS_ALERT_AFTER_S = int(os.environ.get("RECALL_INIT_NATS_ALERT_AFTER_S", "300"))

# How often to re-publish the "recall is still stuck" alert while the init
# loop continues to fail. Spacing this out avoids spamming #maki-general
# every retry, but keeps a periodic poke so the channel doesn't go silent
# after the first alert and let the incident scroll off. See #258.
INIT_NATS_REALERT_INTERVAL_S = int(os.environ.get("RECALL_INIT_NATS_REALERT_INTERVAL_S", "1800"))

# Suggested step 5 in #258: after a very-long-stuck threshold, deliberately
# exit so k8s reschedules the pod — sometimes the right answer is "give up,
# get a fresh pod, hope it sticks". Default 2h — long enough that legitimate
# slow boots survive but well short of the multi-day silent hangs the issue
# was written about. Set the env var to 0 to disable.
INIT_FATAL_EXIT_AFTER_S = int(os.environ.get("RECALL_INIT_FATAL_EXIT_AFTER_S", "7200"))

# Per-attempt timeout for ``Memory.from_config`` (#312). Without this, a
# half-open TCP socket / DNS wedge / "accepted but never replies" backend
# can hang the to_thread call forever — the except branch never runs, so
# none of the safety nets (NATS stuck alert, INIT_FATAL_EXIT_AFTER_S,
# telemetry counters) ever engage. Default 120s: comfortably larger than
# a healthy boot (typically <30s) so a slow-but-recovering dependency
# doesn't flap, but well under INIT_FATAL_EXIT_AFTER_S so multiple
# timed-out attempts roll up into the fatal-exit window. Set to 0 to
# disable (kept for emergency-revert; not recommended).
INIT_ATTEMPT_TIMEOUT_S = int(os.environ.get("RECALL_INIT_ATTEMPT_TIMEOUT_S", "120"))

# Connect-timeout passed through to the pgvector libpq connection string so
# a wedged Postgres also fails fast at the driver layer instead of relying
# solely on the asyncio.wait_for wrapper above. See #312. Bounded small —
# every retry pays this cost on top of the normal backoff.
PG_CONNECT_TIMEOUT_S = int(os.environ.get("RECALL_PG_CONNECT_TIMEOUT_S", "10"))

# Per-query bound on the pgvector /health probe. ``connect_timeout`` only
# bounds the libpq handshake — once the TCP+TLS is established, the cursor
# ``execute``/``fetchone`` can block until kernel TCP retransmit gives up
# (typically 60–120s). That's exactly the failure mode that hung /health
# while curl-by-hand sometimes returned 200, splitting recall vs. immune
# health perception (#297). Statement timeout is enforced server-side via
# libpq's ``options`` startup parameter. See #337.
PG_PROBE_STATEMENT_TIMEOUT_MS = int(os.environ.get("RECALL_PG_PROBE_STATEMENT_TIMEOUT_MS", "3000"))

# Per-tx bound on the Neo4j /health probe. The probe driver is cached in
# a module global to avoid re-handshaking per /health call — so a Neo4j
# that wedges *after* the driver warmed up never trips the constructor's
# ``connection_timeout`` again. Setting a tx-level timeout makes the
# server itself kill the RETURN 1 if it doesn't complete in this many
# seconds. See #337.
NEO4J_PROBE_TX_TIMEOUT_S = float(os.environ.get("RECALL_NEO4J_PROBE_TX_TIMEOUT_S", "3.0"))

# Outer wall-clock deadline for each probe inside /health. Backstop in
# case a future probe forgets its own per-call timeout or the wedge sits
# below the SQL/cypher layer (kernel TCP retransmit, driver bug). Keeps
# /health from inheriting the kernel TCP-timeout window. Picked a touch
# larger than the per-probe layer-specific timeouts so the SQL/cypher
# kill path runs first when both could fire. See #337.
PROBE_DEADLINE_S = float(os.environ.get("RECALL_PROBE_DEADLINE_S", "5.0"))


def _build_pg_uri() -> str:
    # Thin wrapper around maki_common.build_pg_dsn — kept as a local
    # helper so the two libpq-specific tunings recall wants (a fleet-wide
    # HA-aware DSN plus a URI-level connect_timeout for #312) live in one
    # place at the call sites. The single-vs-multi-host, URL-encoding,
    # and target_session_attrs behavior all live in maki_common now
    # (see #130 — stem's local builder used to string-interpolate a
    # comma-separated POSTGRES_HOST straight into the URL).
    return build_pg_dsn(connect_timeout=PG_CONNECT_TIMEOUT_S)


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

# NATS handle used by the init loop to escalate "still stuck" alerts onto
# the same subject immune publishes component escalations to (#258). Kept
# optional — if NATS itself is the dependency that's down, init must still
# log/retry and not crash on a None ``_nc``. Set by ``lifespan``.
_nc: Any = None


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
    "last_alert_at": 0.0,  # wall-clock of last NATS alert published; 0 until first alert (#258)
    "alert_count": 0,  # cumulative number of "stuck" alerts published this lifetime (#258)
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


def _likely_dependency(err_text: str, phase: str | None) -> str:
    """Best-effort attribution of an init error to a specific dependency.

    ``_build_config`` knows which URL each dependency lives at (pgvector,
    embedder, synapse, neo4j); the error string usually contains the host
    or a recognisable backend name. This gives the NATS alert and /health
    a useful "looks like X is down" hint without parsing tracebacks. See
    #258 — the bare `%s` format used to flatten this away.
    """
    if phase == "graph":
        return "neo4j"
    lower = err_text.lower()
    # Order matters: synapse is an OpenAI-compatible LLM proxy so "openai"
    # tokens point there; embedder uses ollama; pg is the catch-all backend.
    if "neo4j" in lower:
        return "neo4j"
    if "ollama" in lower or "embed" in lower or "11434" in lower:
        return "embedder"
    if "synapse" in lower or "openai" in lower or "completion" in lower:
        return "synapse"
    if "psycopg" in lower or "postgres" in lower or "pgvector" in lower or "5432" in lower:
        return "pgvector"
    return "unknown"


async def _publish_init_alert(alert_text: str, payload_extra: dict[str, Any]) -> None:
    """Publish a structured init-stuck alert to the immune alert subject.

    Best-effort and crash-safe: if NATS itself is unreachable (e.g. it's the
    dependency that's also wedged), we swallow the publish error so the
    retry loop keeps trying the actual init. Publishes core NATS (not JS)
    so we don't accidentally block on stream creation racing with immune's
    own bootstrap; the ears alert consumer already listens both ways via
    the same subject.

    See #258 — the whole point of this channel is that stem/cortex/ears can
    observe "recall is wedged" without having to read immune's internal
    state dict.
    """
    nc = _nc
    if nc is None:
        log.debug("Cannot publish recall init alert — NATS not connected yet")
        return
    payload = {"alert": alert_text, "timestamp": time.time(), **payload_extra}
    try:
        await nc.publish(IMMUNE_ALERT, json.dumps(payload).encode())
        _init_state["last_alert_at"] = time.time()
        _init_state["alert_count"] += 1
        log.info(
            "recall init-stuck alert published",
            extra={
                "alert_preview": alert_text[:120],
                "alert_count": _init_state["alert_count"],
                "subject": IMMUNE_ALERT,
            },
        )
    except Exception as e:
        # NATS itself is unhappy — don't let a broken alert path mask the
        # underlying init failure. Just log and continue retrying init.
        log.warning("Failed to publish recall init-stuck alert", extra={"error": _format_exception(e)})


async def _maybe_alert_init_stuck(
    *,
    err_text: str,
    attempt: int,
    stuck_for_s: float,
    phase: str,
    backoff_s: int,
) -> None:
    """Fire (or re-fire) a NATS alert if the init has been stuck long enough.

    Gated by two thresholds so a fast-recovering boot stays silent and a
    multi-hour wedge keeps a periodic alert flowing to ears:
      * first alert only once cumulative stuck time ≥ ``INIT_NATS_ALERT_AFTER_S``
      * subsequent alerts spaced by ``INIT_NATS_REALERT_INTERVAL_S``

    The published text deliberately uses the same "STUCK: ..." prefix
    convention immune uses for stuck-pod escalations so ears' formatting
    and human pattern-matching carry over.
    """
    if stuck_for_s < INIT_NATS_ALERT_AFTER_S:
        return
    last = _init_state["last_alert_at"] or 0.0
    if last and (time.time() - last) < INIT_NATS_REALERT_INTERVAL_S:
        return
    dep = _likely_dependency(err_text, phase)
    stuck_min = round(stuck_for_s / 60, 1)
    alert_text = (
        f"STUCK: maki-recall init has been failing for {stuck_min}min "
        f"(attempts={attempt}, phase={phase}, likely_dependency={dep}). "
        f"Last error: {err_text}. Retrying every ≤{backoff_s}s. "
        f"/health returns 503 status=initializing; data endpoints are 503."
    )
    await _publish_init_alert(
        alert_text,
        {
            "component": "maki-recall",
            "kind": "init_stuck",
            "phase": phase,
            "attempts": attempt,
            "stuck_for_s": round(stuck_for_s, 1),
            "likely_dependency": dep,
            "last_error": err_text,
            "next_retry_in_s": backoff_s,
        },
    )


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
            # Per-attempt timeout (#312): without this wrapper a hang inside
            # Memory.from_config (half-open TCP socket, DNS wedge, server in
            # a state that accepts the connection but never replies) never
            # raises, so the except branch — and with it the NATS stuck
            # alert and INIT_FATAL_EXIT_AFTER_S safety net — never fires.
            if INIT_ATTEMPT_TIMEOUT_S > 0:
                vector_mem = await asyncio.wait_for(
                    asyncio.to_thread(Memory.from_config, vector_only_config),
                    timeout=INIT_ATTEMPT_TIMEOUT_S,
                )
            else:
                vector_mem = await asyncio.to_thread(Memory.from_config, vector_only_config)
        except Exception as vec_err:
            # Foundational backend down (pgvector / embedder / LLM). Do NOT
            # blame the graph store — that misattribution was #198.
            # asyncio.wait_for raises bare TimeoutError on timeout; format
            # it with the per-attempt budget so /health, NATS alerts, and
            # logs distinguish a hang from a synchronous backend exception.
            duration = time.time() - attempt_started
            if isinstance(vec_err, asyncio.TimeoutError):
                err_text = (
                    f"TimeoutError: Memory.from_config (vector) exceeded per-attempt "
                    f"timeout of {INIT_ATTEMPT_TIMEOUT_S}s "
                    f"(RECALL_INIT_ATTEMPT_TIMEOUT_S) — backend hung, see #312"
                )
            else:
                err_text = _format_exception(vec_err)
            _init_state["last_error"] = err_text
            _init_state["last_error_at"] = time.time()
            _init_state["last_attempt_duration_s"] = round(duration, 3)
            _init_state["last_attempt_phase"] = "vector"
            stuck_for = time.time() - _init_state["started_at"]
            likely_dep = _likely_dependency(err_text, "vector")
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
                    "likely_dependency": likely_dep,
                },
            )
            # NATS escalation (#258): once we've been stuck past the alert
            # threshold, periodically publish to the same subject ears reads
            # so a recall-wedge is visible from #maki-general without anyone
            # having to read immune's internal state.
            await _maybe_alert_init_stuck(
                err_text=err_text,
                attempt=attempt,
                stuck_for_s=stuck_for,
                phase="vector",
                backoff_s=backoff,
            )
            # Last-resort safety net (#258 step 5): if init has been wedged
            # past the fatal threshold, deliberately exit so k8s reschedules
            # the pod. The 79h silent-hang incident only ended when someone
            # manually deleted the pod; a fresh container is sometimes the
            # difference between "stays stuck" and "comes up clean".
            if INIT_FATAL_EXIT_AFTER_S > 0 and stuck_for >= INIT_FATAL_EXIT_AFTER_S:
                log.critical(
                    "Mem0 init stuck past fatal threshold — exiting for k8s to reschedule",
                    extra={
                        "stuck_for_s": round(stuck_for, 1),
                        "fatal_threshold_s": INIT_FATAL_EXIT_AFTER_S,
                        "attempts": attempt,
                        "last_error": err_text,
                        "likely_dependency": likely_dep,
                    },
                )
                # Best-effort goodbye alert before we die, with a short
                # timeout so a broken NATS doesn't keep us hanging here too.
                try:
                    await asyncio.wait_for(
                        _publish_init_alert(
                            f"FATAL: maki-recall init stuck for "
                            f"{round(stuck_for / 60, 1)}min; exiting for k8s to reschedule. "
                            f"Last error: {err_text} (likely_dependency={likely_dep})",
                            {
                                "component": "maki-recall",
                                "kind": "init_fatal_exit",
                                "stuck_for_s": round(stuck_for, 1),
                                "attempts": attempt,
                                "likely_dependency": likely_dep,
                                "last_error": err_text,
                            },
                        ),
                        timeout=3.0,
                    )
                except Exception:
                    pass
                # ``os._exit`` rather than ``sys.exit`` — we're inside an
                # asyncio task, and SystemExit raised here would be swallowed
                # by the task boundary (the old #258 failure mode would just
                # continue silently). os._exit unconditionally tears the
                # process down so the kubelet sees a fresh start. The pod's
                # RestartPolicy=Always handles the reschedule.
                os._exit(1)
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
            # Same per-attempt timeout protection as the vector phase (#312).
            # A neo4j server that accepts the TCP connection but never
            # responds to bolt handshake would otherwise hang here forever.
            if INIT_ATTEMPT_TIMEOUT_S > 0:
                graph_mem = await asyncio.wait_for(
                    asyncio.to_thread(Memory.from_config, config),
                    timeout=INIT_ATTEMPT_TIMEOUT_S,
                )
            else:
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
            if isinstance(graph_err, asyncio.TimeoutError):
                err_text = (
                    f"TimeoutError: Memory.from_config (graph) exceeded per-attempt "
                    f"timeout of {INIT_ATTEMPT_TIMEOUT_S}s "
                    f"(RECALL_INIT_ATTEMPT_TIMEOUT_S) — neo4j hung, see #312"
                )
            else:
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
            # Same per-attempt timeout (#312) — a hung neo4j here would
            # otherwise wedge the recovery loop indefinitely with no signal.
            if INIT_ATTEMPT_TIMEOUT_S > 0:
                mem = await asyncio.wait_for(
                    asyncio.to_thread(Memory.from_config, config),
                    timeout=INIT_ATTEMPT_TIMEOUT_S,
                )
            else:
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
            if isinstance(e, asyncio.TimeoutError):
                err_text = (
                    f"TimeoutError: Memory.from_config (graph retry) exceeded "
                    f"per-attempt timeout of {INIT_ATTEMPT_TIMEOUT_S}s "
                    f"(RECALL_INIT_ATTEMPT_TIMEOUT_S) — neo4j hung, see #312"
                )
            else:
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
    global _init_task, _neo4j_probe_driver, _nc
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
    # Best-effort NATS connection so the init loop can publish stuck-init
    # alerts (#258). We do NOT block startup on this — if NATS itself is
    # the dependency that's down, recall must still come up far enough to
    # report 503 status=initializing via /health. ``_publish_init_alert``
    # tolerates ``_nc is None`` and skips publishing in that case.
    try:
        _nc = await connect_nats(NATS_URL, token=NATS_TOKEN, max_retries=3, base_delay=1.0, max_delay=5.0)
    except Exception as e:
        # Don't crash the lifespan — the init loop must still run.
        log.warning(
            "NATS connection failed at startup; init-stuck alerts disabled",
            extra={"nats_url": NATS_URL, "error": _format_exception(e)},
        )
        _nc = None
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
        if _nc is not None:
            try:
                await _nc.close()
            except Exception:
                pass
            _nc = None


app = FastAPI(title="maki-recall", version="0.0.1", lifespan=lifespan)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all 500 handler — log with traceback, return a generic body.

    Centralises the five copy-pasted ``except Exception: raise HTTPException(500,
    detail=str(e))`` blocks the data endpoints used to carry. Two reasons that
    pattern was wrong:

      * ``detail=str(e)`` leaks internal exception strings — DB URIs, credential
        fragments, stack hints — to whatever calls /memories. The generic body
        here exposes only the HTTP status text.
      * Every error became a 500 regardless of cause. ``HTTPException`` raised
        explicitly by handlers (e.g. the 400 from ``_require_identifier`` or
        the 503 from ``_require_memory``) is dispatched by FastAPI's own more-
        specific handler and never reaches this one, so 4xx responses still
        work as written.

    The traceback is still captured in logs via ``log.exception``, which is
    what the per-endpoint blocks did before — just once, in one place.
    """
    log.exception(
        "Unhandled error on %s %s",
        request.method,
        request.url.path,
        extra={"error_type": type(exc).__name__},
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


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


def _require_identifier(
    user_id: str | None,
    agent_id: str | None,
    run_id: str | None,
) -> None:
    """Raise 400 unless at least one mem0 entity identifier is provided.

    Mem0's storage model is keyed by ``user_id``/``agent_id``/``run_id``; a
    request with none of them set has no scope and would either error deep
    inside mem0 or — worse — operate over every entity. Reject early with
    a clean 4xx instead of letting the catch-all handler return 500.
    """
    if not any([user_id, agent_id, run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")


def _identifier_params(
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return a kwargs dict of the non-None mem0 identifier params.

    Centralises the ``{k: v for k, v in {...}.items() if v is not None}``
    comprehension that used to live in four endpoints. Suitable both for
    direct kwarg-splat into ``mem.get_all``/``mem.delete_all`` and for
    building the ``filters={}`` dict that ``mem.search`` expects.

    Return type is ``dict[str, Any]`` rather than ``dict[str, str]`` so
    callers can extend the dict with non-string mem0 kwargs (e.g.
    ``metadata=dict[str, Any]`` in ``add_memory``) without a cast.
    """
    return {k: v for k, v in (("user_id", user_id), ("agent_id", agent_id), ("run_id", run_id)) if v is not None}


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

    Per-query bound (#337): ``connect_timeout=3`` only caps the libpq
    handshake. Once the connection is up, ``execute``/``fetchone`` can
    block on a server-side wedge until kernel TCP retransmit gives up
    (~60–120s) — which is the exact /health hang we were eating. The
    ``options="-c statement_timeout=..."`` startup parameter caps every
    statement on this connection server-side, so the SELECT 1 cannot
    outrun the deadline regardless of what the driver does locally.
    """
    try:
        with psycopg.connect(
            _build_pg_uri(),
            connect_timeout=3,
            options=f"-c statement_timeout={PG_PROBE_STATEMENT_TIMEOUT_MS}",
        ) as conn:
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

    Per-call bound (#337): the cached driver means ``connection_timeout``
    only fires on the *first* probe — subsequent probes skip the connect
    path entirely. A Neo4j that goes wedged after the driver warmed up
    has nothing to time out, so ``session.run(...).consume()`` could
    hang until OS-level TCP keepalives fired (which produces exactly the
    "consecutive failures climbing while curl sometimes returns 200"
    split between recall and immune we observed in #297). The fix:
    bound the transaction at the server with ``begin_transaction(
    timeout=...)``, and additionally cap connection-pool acquisition so
    a wedged-pool case can't sit forever waiting for a free conn either.
    The outer /health ``asyncio.wait_for`` in :func:`_run_probe_with_deadline`
    handles wedges below the bolt-protocol layer.
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
                # Cap how long acquiring a pooled connection can wait —
                # without this, a wedged pool can swallow the entire
                # outer probe deadline before we even see a tx start.
                connection_acquisition_timeout=NEO4J_PROBE_TX_TIMEOUT_S,
            )
        with _neo4j_probe_driver.session() as session:
            with session.begin_transaction(timeout=NEO4J_PROBE_TX_TIMEOUT_S) as tx:
                tx.run("RETURN 1 AS ok").consume()
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


async def _run_probe_with_deadline(probe: Any, name: str) -> str | None:
    """Run a sync reachability probe under an outer asyncio deadline.

    The per-driver SQL/cypher timeouts (PG_PROBE_STATEMENT_TIMEOUT_MS,
    NEO4J_PROBE_TX_TIMEOUT_S) are the primary defense — they kill a
    wedged query server-side. This wrapper is the backstop for wedges
    that sit *below* the SQL/cypher layer (kernel TCP retransmit, a
    driver-internal lock, future probe code that forgets its own
    timeout). Without it, /health inherits the kernel TCP-timeout
    window (~60–120s), which is what produced the recall↔immune
    split-perception incident in #297. See #337.

    On a neo4j timeout we drop the cached driver — the existing except
    branch in :func:`_probe_neo4j` already does that on
    ``Exception``, but a hung ``session.run`` never reaches its except.
    pgvector opens a fresh psycopg.connect per call so there's no
    cached state to invalidate there.

    Note: ``asyncio.to_thread`` cannot cancel the underlying OS thread
    on timeout — the thread keeps running until its server-side timeout
    fires (or the kernel gives up on the socket). That's an acceptable
    leak: the next /health call sees ``_neo4j_probe_driver is None``
    and reconnects, and the orphaned thread eventually unwinds.
    """
    global _neo4j_probe_driver
    try:
        return await asyncio.wait_for(asyncio.to_thread(probe), timeout=PROBE_DEADLINE_S)
    except TimeoutError:
        if name == "neo4j" and _neo4j_probe_driver is not None:
            try:
                _neo4j_probe_driver.close()
            except Exception:
                pass
            _neo4j_probe_driver = None
        return f"TimeoutError: {name} probe exceeded {PROBE_DEADLINE_S}s deadline"


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
        # #258: number of "stuck" NATS alerts published this lifetime so a
        # human eyeballing /health knows whether the channel has already
        # been told, and how loud the situation has been.
        "init_alert_count": _init_state["alert_count"],
    }
    if _init_state["last_error"] is not None and _init_state["last_error_at"]:
        snap["last_error_age_s"] = round(now - _init_state["last_error_at"], 1)
    # #339: surface ``ready_age_s`` so a curl /health distinguishes "just
    # became ready, watch closely" from "ready for hours, stable". The
    # ``ready_at`` field was written in three sites but never read until
    # now — was dead telemetry.
    ready_at = _init_state["ready_at"] or 0.0
    if ready_at:
        snap["ready_age_s"] = round(now - ready_at, 1)
    if memory is None and init_duration_s >= INIT_STUCK_THRESHOLD_S:
        snap["init_stuck"] = True
        # Surface the inferred dependency so a curl /health gives the same
        # "looks like X is down" hint the NATS alert carries.
        if _init_state["last_error"]:
            snap["likely_dependency"] = _likely_dependency(_init_state["last_error"], _init_state["last_attempt_phase"])
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
async def live() -> dict[str, str]:
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

    ``async def`` per #339: a sync ``def`` handler runs in Starlette's
    anyio worker threadpool (default 40 threads), which the data-path
    handlers (``/memories``, ``/search``) also share. Under sustained
    or wedged traffic those handlers can saturate the pool, leaving
    ``/live`` requests stuck in the queue — exactly the kubelet-timeout
    -> crashloop failure mode this endpoint was split out to prevent.
    Returning immediately from the event loop avoids the threadpool
    entirely.
    """
    return {"status": "alive"}


@app.get("/health")
async def health():
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

    Async (#337) so each probe runs under ``asyncio.wait_for`` — a
    wedged backend (TCP accepted but SQL/cypher hangs) can no longer
    inflate /health's response time past PROBE_DEADLINE_S. That was
    the root cause of the recall↔immune health-perception split in
    #297.
    """
    mem = memory
    if mem is None:
        body: dict[str, Any] = {"status": "initializing", "graph": False}
        body.update(_init_snapshot())
        return JSONResponse(status_code=503, content=body)

    checks: dict[str, Any] = {"status": "ok", "graph": graph_enabled}
    checks.update(_init_snapshot())

    pg_err = await _run_probe_with_deadline(_probe_pgvector, "pgvector")
    if pg_err is not None:
        log.warning("recall /health pg probe failed: %s", pg_err)
        checks["status"] = "degraded"
        checks["pg"] = "down"

    if graph_enabled:
        neo_err = await _run_probe_with_deadline(_probe_neo4j, "neo4j")
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
    _require_identifier(req.user_id, req.agent_id, req.run_id)
    params = _identifier_params(req.user_id, req.agent_id, req.run_id)
    if req.metadata is not None:
        params["metadata"] = req.metadata
    return JSONResponse(content=mem.add(messages=req.messages, **params))


@app.get("/memories")
def get_memories(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    mem = _require_memory()
    _require_identifier(user_id, agent_id, run_id)
    return mem.get_all(**_identifier_params(user_id, agent_id, run_id))


@app.post("/search")
def search_memories(req: SearchRequest):
    mem = _require_memory()
    # mem0 requires entity identifiers (user_id, agent_id, run_id) inside filters={},
    # not as top-level kwargs — only limit stays top-level.
    filters = _identifier_params(req.user_id, req.agent_id, req.run_id)
    kwargs: dict[str, Any] = {}
    if filters:
        kwargs["filters"] = filters
    if req.limit is not None:
        kwargs["limit"] = req.limit
    return mem.search(query=req.query, **kwargs)


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    mem = _require_memory()
    mem.delete(memory_id=memory_id)
    return {"message": "Memory deleted"}


@app.delete("/memories")
def delete_all_memories(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    mem = _require_memory()
    _require_identifier(user_id, agent_id, run_id)
    mem.delete_all(**_identifier_params(user_id, agent_id, run_id))
    return {"message": "All memories deleted"}


def cli():
    import uvicorn

    uvicorn.run("maki_recall.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
