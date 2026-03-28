"""NATS connection and KV bucket utilities."""

from __future__ import annotations

import json
import logging
from typing import Any

import nats
from nats.aio.client import Client
from nats.js.kv import KeyValue

log = logging.getLogger(__name__)


async def connect_nats(url: str) -> Client:
    """Connect to NATS and return the client."""
    nc = await nats.connect(url)
    log.info("Connected to NATS", extra={"nats_url": url})
    return nc


async def init_kv(js, bucket: str, defaults: dict[str, Any] | None = None) -> KeyValue:
    """Create or connect to a KV bucket, optionally seeding defaults.

    Args:
        js: JetStream context.
        bucket: Bucket name.
        defaults: If provided, seed these key/value pairs if they don't exist.
            Values are JSON-encoded before storing.
    """
    try:
        kv = await js.key_value(bucket)
    except Exception:
        kv = await js.create_key_value(bucket=bucket)
        log.info("Created KV bucket", extra={"bucket": bucket})

    if defaults:
        for key, value in defaults.items():
            try:
                await kv.get(key)
            except Exception:
                await kv.put(key, json.dumps(value).encode())
                log.info("Seeded KV default", extra={"bucket": bucket, "key": key, "value": value})

    return kv


async def load_kv_config(kv: KeyValue, defaults: dict[str, Any]) -> dict[str, Any]:
    """Load config from a KV bucket, falling back to provided defaults."""
    config = {}
    for key, default in defaults.items():
        try:
            entry = await kv.get(key)
            config[key] = json.loads(entry.value.decode())
        except Exception:
            config[key] = default
    return config
