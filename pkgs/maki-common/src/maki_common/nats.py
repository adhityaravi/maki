"""NATS connection and KV bucket utilities."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import nats
from nats.aio.client import Client
from nats.js.kv import KeyValue

log = logging.getLogger(__name__)


async def connect_nats(url: str, token: str | None = None) -> Client:
    """Connect to NATS and return the client."""
    kwargs: dict[str, Any] = {}
    if token:
        kwargs["token"] = token
    nc = await nats.connect(url, **kwargs)
    log.info("Connected to NATS", extra={"nats_url": url, "auth": bool(token)})
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


async def try_claim_loop(kv: KeyValue, key: str, interval: float, instance_id: str) -> bool:
    """Try to claim a periodic loop iteration via NATS KV CAS.

    Prevents multiple instances from running the same timed loop concurrently.
    Uses optimistic locking — first instance to update wins, others skip.

    Args:
        kv: KV bucket (e.g. maki-lock).
        key: Claim key (e.g. "loop.stem.idle").
        interval: Minimum seconds between claims.
        instance_id: Unique ID for this process instance.

    Returns:
        True if this instance should run, False if another claimed it.
    """
    now = time.time()
    claim = json.dumps({"instance": instance_id, "claimed_at": now}).encode()

    try:
        entry = await kv.get(key)
        data = json.loads(entry.value.decode())
        if now - data.get("claimed_at", 0) < interval:
            return False
        # Claim expired — try to take over via CAS
        await kv.update(key, claim, entry.revision)
        return True
    except nats.js.errors.KeyNotFoundError:
        try:
            await kv.create(key, claim)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def kv_put_float(kv: KeyValue, key: str, value: float) -> None:
    """Store a float in NATS KV."""
    await kv.put(key, json.dumps(value).encode())


async def kv_get_float(kv: KeyValue, key: str, default: float = 0.0) -> float:
    """Read a float from NATS KV."""
    try:
        entry = await kv.get(key)
        return json.loads(entry.value.decode())
    except Exception:
        return default
