"""maki-recall: Memory service backed by Mem0.

Provides REST API for memory storage, search, and retrieval
using pgvector + Neo4j graph store + Ollama embeddings.
"""

import logging
import os
from typing import Any
from urllib.parse import quote_plus

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from maki_common import configure_logging
from mem0 import Memory
from pydantic import BaseModel, Field

configure_logging()
log = logging.getLogger(__name__)


def _build_pg_uri() -> str:
    user = quote_plus(os.environ.get("POSTGRES_USER", "maki"))
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    hosts = os.environ.get("POSTGRES_HOST", "maki-vault")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "maki")
    host_port = ",".join(f"{h}:{port}" for h in hosts.split(","))
    return f"postgresql://{user}:{password}@{host_port}/{db}?target_session_attrs=read-write"


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
graph_enabled = False

if neo4j_uri:
    config["graph_store"] = {
        "provider": "neo4j",
        "config": {
            "url": neo4j_uri,
            "username": os.environ.get("NEO4J_USERNAME", "neo4j"),
            "password": os.environ.get("NEO4J_PASSWORD", ""),
        },
    }

log.info(
    "Initializing Mem0",
    extra={
        "vector_store": "pgvector",
        "graph_store": "neo4j" if neo4j_uri else "disabled",
        "llm_provider": config["llm"]["provider"],
        "llm_model": config["llm"]["config"]["model"],
        "embedder_model": config["embedder"]["config"]["model"],
    },
)

# Phase 1: init vector store (pgvector + llm + embedder) without graph.
# By separating vector init from graph init, a pgvector or embedder failure
# propagates its real error message instead of being masked by a broad handler.
graph_enabled = "graph_store" in config
if graph_enabled:
    vector_config = {k: v for k, v in config.items() if k != "graph_store"}
    memory = Memory.from_config(vector_config)
else:
    memory = Memory.from_config(config)

# Phase 2: try graph store (Neo4j) as an optional add-on
if graph_enabled:
    try:
        memory = Memory.from_config(config)
    except Exception:
        log.warning("Graph store unreachable, falling back to vector-only")
        del config["graph_store"]
        graph_enabled = False

app = FastAPI(title="maki-recall", version="0.0.1")


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


def _probe_pgvector() -> str | None:
    """Cheapest possible pgvector reachability check.

    Returns None on success, an error string on failure. Uses the PGVector
    backend's own cursor context manager so we exercise the real connection
    pool used by reads/writes — not a side-channel connection that could be
    healthy while the pool is exhausted or the primary has flipped.
    """
    try:
        with memory.vector_store._get_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def _probe_neo4j() -> str | None:
    """Cheapest possible Neo4j reachability check.

    Returns None on success, an error string on failure. Uses the Langchain
    Neo4jGraph wrapper that mem0 itself drives, so a green probe means the
    same driver our writes go through is reachable.
    """
    try:
        memory.graph.graph.query("RETURN 1 AS ok")
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


@app.get("/health")
def health():
    """Liveness/readiness probe.

    Returns 200 when the underlying stores are usable and 503 when they are
    not, so k8s liveness probes and maki-immune can act on a real signal
    instead of "FastAPI is alive". See issue #169.
    """
    checks: dict[str, Any] = {"status": "ok", "graph": graph_enabled}

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

    if checks["status"] != "ok":
        return JSONResponse(status_code=503, content=checks)
    return checks


@app.post("/memories")
def add_memory(req: MemoryCreate):
    if not any([req.user_id, req.agent_id, req.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")
    params = {k: v for k, v in req.model_dump().items() if v is not None and k != "messages"}
    try:
        return JSONResponse(content=memory.add(messages=req.messages, **params))
    except Exception as e:
        log.exception("Error adding memory")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/memories")
def get_memories(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    if not any([user_id, agent_id, run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")
    params = {k: v for k, v in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if v is not None}
    try:
        return memory.get_all(**params)
    except Exception as e:
        log.exception("Error getting memories")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search")
def search_memories(req: SearchRequest):
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
        return memory.search(query=req.query, **kwargs)
    except Exception as e:
        log.exception("Error searching memories")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    try:
        memory.delete(memory_id=memory_id)
        return {"message": "Memory deleted"}
    except Exception as e:
        log.exception("Error deleting memory")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/memories")
def delete_all_memories(user_id: str | None = None, agent_id: str | None = None, run_id: str | None = None):
    if not any([user_id, agent_id, run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier required.")
    params = {k: v for k, v in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if v is not None}
    try:
        memory.delete_all(**params)
        return {"message": "All memories deleted"}
    except Exception as e:
        log.exception("Error deleting memories")
        raise HTTPException(status_code=500, detail=str(e))


def cli():
    import uvicorn

    uvicorn.run("maki_recall.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
