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

config = {
    "version": "v1.1",
    "vector_store": {
        "provider": "pgvector",
        "config": {
            "collection_name": os.environ.get("POSTGRES_COLLECTION_NAME", "memories"),
            "embedding_model_dims": int(os.environ.get("EMBEDDING_DIMS", "768")),
            "connection_string": "postgresql://{}:{}@{}:{}/{}".format(
                quote_plus(os.environ.get("POSTGRES_USER", "maki")),
                quote_plus(os.environ["POSTGRES_PASSWORD"]),
                os.environ.get("POSTGRES_HOST", "maki-vault"),
                os.environ.get("POSTGRES_PORT", "5432"),
                os.environ.get("POSTGRES_DB", "maki"),
            ),
        },
    },
    "graph_store": {
        "provider": "neo4j",
        "config": {
            "url": os.environ.get("NEO4J_URI", "bolt://maki-graph:7687"),
            "username": os.environ.get("NEO4J_USERNAME", "neo4j"),
            "password": os.environ["NEO4J_PASSWORD"],
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

log.info(
    "Initializing Mem0",
    extra={
        "vector_store": "pgvector",
        "graph_store": "neo4j",
        "llm_provider": config["llm"]["provider"],
        "llm_model": config["llm"]["config"]["model"],
        "embedder_model": config["embedder"]["config"]["model"],
    },
)

memory = Memory.from_config(config)

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


@app.get("/health")
def health():
    return {"status": "ok"}


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
    params = {k: v for k, v in req.model_dump().items() if v is not None and k != "query"}
    try:
        return memory.search(query=req.query, **params)
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
