FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc libc6-dev && \
    pip install --no-cache-dir \
    psycopg2-binary \
    "mem0ai[graph]" \
    "psycopg[binary,pool]" \
    rank-bm25 \
    langchain-neo4j \
    neo4j \
    ollama \
    fastapi \
    uvicorn && \
    apt-get purge -y gcc && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY main.py .

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
