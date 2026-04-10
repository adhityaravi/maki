# ── Stage 1: Export ProsusAI/finbert to FP32 ONNX, then quantize to INT8 ──────
FROM python:3.12-slim AS exporter

# Install torch (CPU-only) first so pip resolves it from the PyTorch index,
# then install optimum exporters + transformers on top.
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.5" && \
    pip install --no-cache-dir \
        "optimum[exporters]>=1.24" \
        "transformers>=4.47"

# Export to FP32 ONNX via Python API (avoids optimum-cli argument parsing issues)
COPY images/maki-finbert/export.py /tmp/export.py
RUN python3 /tmp/export.py

# Quantize to INT8 (dynamic, AVX2-optimised for x86_64)
COPY images/maki-finbert/quantize.py /tmp/quantize.py
RUN python3 /tmp/quantize.py

# ── Stage 2: Lean runtime — no torch ──────────────────────────────────────────
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv pip install --system \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "transformers>=4.47" \
        "optimum[onnxruntime]>=1.24"

WORKDIR /app

# Bake the INT8 model in — no network calls at startup
COPY --from=exporter /app/model /app/model

COPY images/maki-finbert/app.py .

RUN useradd -m -s /bin/bash finbert
USER finbert

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
