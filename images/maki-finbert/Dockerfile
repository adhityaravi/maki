# ── Stage 1: Export ProsusAI/finbert to FP32 ONNX, then quantize to INT8 ──────
FROM python:3.12-slim AS exporter

# Install torch (CPU-only) first so pip resolves it from the PyTorch index,
# then install optimum with both exporters and onnxruntime extras + transformers.
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.5" && \
    pip install --no-cache-dir \
        "optimum[exporters,onnxruntime]>=1.24" \
        "transformers>=4.47"

# Export to ONNX via the stable high-level ORT API
COPY images/maki-finbert/export.py /tmp/export.py
RUN python3 /tmp/export.py

# Quantize to INT8 (dynamic, AVX2-optimised for x86_64)
COPY images/maki-finbert/quantize.py /tmp/quantize.py
RUN python3 /tmp/quantize.py

# ── Stage 2: Lean runtime — no torch ──────────────────────────────────────────
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /usr/local/bin/uvx

COPY pkgs/maki-finbert /build/pkgs/maki-finbert
RUN uv pip install --system /build/pkgs/maki-finbert \
    && rm -rf /build

WORKDIR /app

# Bake the INT8 model in — no network calls at startup
COPY --from=exporter /app/model /app/model

RUN useradd -m -s /bin/bash finbert
USER finbert

EXPOSE 8080

CMD ["maki-finbert"]
