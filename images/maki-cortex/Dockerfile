FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    nats-py \
    claude-agent-sdk

COPY main.py .

RUN useradd -m -s /bin/bash cortex
USER cortex

EXPOSE 8080

CMD ["python", "main.py"]
