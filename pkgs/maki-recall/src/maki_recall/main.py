"""maki-recall: Memory service backed by Mem0.

Provides REST API for memory storage, search, and retrieval
using pgvector + Neo4j graph store + Ollama embeddings.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

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
GRAPH_RETRY_INTERVAL_SECONDS = 60


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


async def _init_mem0() -> None:
    """Initialise Mem0 in the background, retrying on failure.

    On success, sets the module globals ``memory`` and ``graph_enabled``.
    If the graph store is unreachable but the vector backend is reachable,
    we fall back to vector-only and schedule ``_retry_graph_init`` so a
    transient Neo4j outage doesn't permanently downgrade the service.
    """
    global memory, graph_enabled, _graph_retry_task
    backoff = INIT_RETRY_SECONDS
    while True:
        config, neo4j_requested = _build_config()
        log.info(
            "Initializing Mem0",
            extra={
                "vector_store": "pgvector",
                "graph_store": "neo4j" if neo4j_requested else "disabled",
                "llm_provider": config["llm"]["provider"],
                "llm_model": config["llm"]["config"]["model"],
                "embedder_model": config["embedder"]["config"]["model"],
            },
        )
        try:
            mem = await asyncio.to_thread(Memory.from_config, config)
            memory = mem
            graph_enabled = neo4j_requested
            log.info("Mem0 ready", extra={"graph_enabled": graph_enabled})
            return
        except Exception as full_err:
            if neo4j_requested:
                log.warning(
                    "Mem0 init with graph store failed (%s) — trying vector-only fallback",
                    full_err,
                )
                vector_only = {k: v for k, v in config.items() if k != "graph_store"}
                try:
                    mem = await asyncio.to_thread(Memory.from_config, vector_only)
                    memory = mem
                    graph_enabled = False
                    log.info("Mem0 ready in vector-only mode; will retry graph init in background")
                    if _graph_retry_task is None or _graph_retry_task.done():
                        _graph_retry_task = asyncio.create_task(_retry_graph_init(), name="recall.graph_retry")
                    return
                except Exception as vec_err:
                    log.warning(
                        "Vector-only init also failed: %s — retrying full init in %ds",
                        vec_err,
                        backoff,
                    )
            else:
                log.warning("Mem0 init failed: %s — retrying in %ds", full_err, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, INIT_RETRY_MAX_SECONDS)


async def _retry_graph_init() -> None:
    """Periodically attempt to upgrade vector-only Mem0 to graph-enabled.

    Runs only while ``graph_enabled`` is False but the env still asks for
    Neo4j. On success it atomically swaps in a new graph-enabled Memory
    instance so subsequent writes/searches include the graph store.
    """
    global memory, graph_enabled
    while not graph_enabled:
        await asyncio.sleep(GRAPH_RETRY_INTERVAL_SECONDS)
        config, neo4j_requested = _build_config()
        if not neo4j_requested:
            # Neo4j was removed from config — no graph to recover.
            log.info("Graph retry exiting: NEO4J_URI no longer set")
            return
        try:
            mem = await asyncio.to_thread(Memory.from_config, config)
            memory = mem
            graph_enabled = True
            log.info("Mem0 graph store recovered; graph_enabled=True")
            return
        except Exception as e:
            log.debug("Graph store still unreachable: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spin up Mem0 initialization as a background task on startup.

    Init runs in the background so the FastAPI app registers immediately
    and /health can return 503 "initializing" while the underlying stores
    come up. See issue #135 — the previous module-import-time init caused
    k8s crashloops with no readiness signal whenever pgvector / embedder /
    Neo4j was briefly unreachable at boot.
    """
    global _init_task
    log.info("maki-recall starting; scheduling Mem0 init")
    _init_task = asyncio.create_task(_init_mem0(), name="recall.mem0_init")
    try:
        yield
    finally:
        for task in (_init_task, _graph_retry_task):
            if task is not None and not task.done():
                task.cancel()


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


def _probe_pgvector(mem: Memory) -> str | None:
    """Cheapest possible pgvector reachability check.

    Returns None on success, an error string on failure. Uses the PGVector
    backend's own cursor context manager so we exercise the real connection
    pool used by reads/writes — not a side-channel connection that could be
    healthy while the pool is exhausted or the primary has flipped.
    """
    try:
        with mem.vector_store._get_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def _probe_neo4j(mem: Memory) -> str | None:
    """Cheapest possible Neo4j reachability check.

    Returns None on success, an error string on failure. Uses the Langchain
    Neo4jGraph wrapper that mem0 itself drives, so a green probe means the
    same driver our writes go through is reachable.
    """
    try:
        mem.graph.graph.query("RETURN 1 AS ok")
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


@app.get("/health")
def health():
    """Liveness/readiness probe.

    Returns 503 with ``status: initializing`` while Mem0 hasn't finished
    booting (so k8s and maki-immune wait instead of crashlooping blind),
    200 when the stores are usable, and 503 ``status: degraded`` when one
    of the backing stores fails its probe. See issues #135 and #169.
    """
    mem = memory
    if mem is None:
        return JSONResponse(status_code=503, content={"status": "initializing", "graph": False})

    checks: dict[str, Any] = {"status": "ok", "graph": graph_enabled}

    pg_err = _probe_pgvector(mem)
    if pg_err is not None:
        log.warning("recall /health pg probe failed: %s", pg_err)
        checks["status"] = "degraded"
        checks["pg"] = "down"

    if graph_enabled:
        neo_err = _probe_neo4j(mem)
        if neo_err is not None:
            log.warning("recall /health neo4j probe failed: %s", neo_err)
            checks["status"] = "degraded"
            checks["neo4j"] = "down"

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
