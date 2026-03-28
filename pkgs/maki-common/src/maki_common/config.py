"""Config tag parsing and self-tuning utilities."""

from __future__ import annotations

import json
import logging
import re

from nats.js.kv import KeyValue

log = logging.getLogger(__name__)


def parse_config_tags(text: str) -> list[tuple[str, str]]:
    """Parse [CONFIG:key=value] tags from text.

    Returns list of (key, raw_value_string) tuples.
    """
    return re.findall(r"\[CONFIG:(\w+)=([^\]]+)\]", text)


def parse_tagged(text: str, tag: str) -> list[str]:
    """Parse [TAG:content] sections from text.

    Args:
        text: The text to parse.
        tag: The tag name (e.g. "DIGEST", "ALERT").

    Returns list of content strings found.
    """
    return [m.strip() for m in re.findall(rf"\[{re.escape(tag)}:(.*?)\]", text, re.DOTALL)]


def strip_tags(text: str) -> str:
    """Remove all [TAG:...] sections from text."""
    return re.sub(r"\[\w+:[^\]]*\]", "", text).strip()


async def apply_config_updates(
    kv: KeyValue,
    updates: list[tuple[str, str]],
    allowed_keys: set[str] | None = None,
) -> None:
    """Apply parsed config updates to a KV bucket.

    Args:
        kv: NATS KV bucket.
        updates: List of (key, raw_value) from parse_config_tags().
        allowed_keys: If provided, only these keys are accepted.
    """
    for key, raw_value in updates:
        if allowed_keys and key not in allowed_keys:
            log.warning("Rejected config update for unknown key", extra={"key": key})
            continue
        try:
            parsed = json.loads(raw_value)
            await kv.put(key, json.dumps(parsed).encode())
            log.info("Config self-tuned", extra={"key": key, "value": parsed})
        except Exception:
            log.warning("Failed to parse config update", extra={"key": key, "raw_value": raw_value})
