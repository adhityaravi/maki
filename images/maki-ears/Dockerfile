FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    discord.py \
    nats-py

COPY main.py .

CMD ["python", "main.py"]
