"""maki-finbert: Lightweight FinBERT sentiment inference service.

Wraps ProsusAI/finbert (INT8 ONNX) in a FastAPI endpoint.
Called by the trading loop for news headline sentiment scoring.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from optimum.onnxruntime import ORTModelForSequenceClassification
from pydantic import BaseModel
from transformers import AutoTokenizer, pipeline

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MODEL_PATH = "/app/model"

_pipe = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pipe
    log.info("Loading FinBERT INT8 ONNX model from %s", MODEL_PATH)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    model = ORTModelForSequenceClassification.from_pretrained(MODEL_PATH)
    _pipe = pipeline(
        "text-classification",
        model=model,
        tokenizer=tokenizer,
        truncation=True,
        max_length=512,
    )
    log.info("FinBERT model ready")
    yield
    _pipe = None


app = FastAPI(title="maki-finbert", version="0.1.0", lifespan=lifespan)


class ScoreRequest(BaseModel):
    texts: list[str]


class SentimentResult(BaseModel):
    label: str  # positive | negative | neutral
    score: float  # confidence for the predicted label (0–1)


class ScoreResponse(BaseModel):
    results: list[SentimentResult]


@app.get("/live")
def live() -> dict[str, Any]:
    """Liveness probe — process-only signal.

    Returns 200 as long as the FastAPI event loop is responsive. Does NOT
    check whether the finbert pipeline is loaded — model load can take
    tens of seconds on cold start and a liveness fail there would crashloop
    the pod just as it's coming up (the #253 pattern, fixed fleet-wide in
    #276). Model-load state is reported via ``/health`` (readiness) only.
    """
    return {"status": "alive"}


@app.get("/health")
def health() -> dict[str, Any]:
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "ok", "model": "ProsusAI/finbert-int8-onnx"}


@app.post("/score")
def score(request: ScoreRequest) -> ScoreResponse:
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if not request.texts:
        return ScoreResponse(results=[])

    raw = _pipe(request.texts)
    results = []
    for item in raw:
        # pipeline with batch input may return list-of-lists or list-of-dicts
        top = item[0] if isinstance(item, list) else item
        results.append(
            SentimentResult(
                label=top["label"].lower(),
                score=round(top["score"], 4),
            )
        )
    return ScoreResponse(results=results)


def cli() -> None:
    uvicorn.run("maki_finbert.main:app", host="0.0.0.0", port=8080)
