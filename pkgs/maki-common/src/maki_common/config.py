"""Config tag parsing and self-tuning utilities."""

from __future__ import annotations

import json
import logging
import re

from nats.js.kv import KeyValue

log = logging.getLogger(__name__)

# Canonical [TAG:content] matcher. Lazy content match with DOTALL so tags may
# span newlines — immune's DIGEST/ALERT payloads are routinely multi-paragraph.
# All tag helpers below share this one shape so parse_tagged and strip_tags
# cannot silently disagree about what "content" means.
_TAG_RE = re.compile(r"\[(\w+):(.*?)\]", re.DOTALL)


def parse_config_tags(text: str) -> list[tuple[str, str]]:
    """Parse [CONFIG:key=value] tags from text.

    Returns list of (key, raw_value_string) tuples.
    """
    out: list[tuple[str, str]] = []
    for tag, content in _TAG_RE.findall(text):
        if tag != "CONFIG":
            continue
        key, sep, raw_value = content.partition("=")
        if not sep:
            continue
        out.append((key, raw_value))
    return out


def parse_tagged(text: str, tag: str) -> list[str]:
    """Parse [TAG:content] sections from text.

    Args:
        text: The text to parse.
        tag: The tag name (e.g. "DIGEST", "ALERT").

    Returns list of content strings found.
    """
    return [content.strip() for found_tag, content in _TAG_RE.findall(text) if found_tag == tag]


def strip_tags(text: str) -> str:
    """Remove all [TAG:...] sections from text."""
    return _TAG_RE.sub("", text).strip()


async def apply_config_updates(
    kv: KeyValue,
    updates: list[tuple[str, str]],
    allowed_keys: set[str] | None = None,
    validators: dict[str, list] | None = None,
) -> None:
    """Apply parsed config updates to a KV bucket.

    Args:
        kv: NATS KV bucket.
        updates: List of (key, raw_value) from parse_config_tags().
        allowed_keys: If provided, only these keys are accepted.
        validators: Optional dict of key → list of allowed values.
            If the parsed value is not in the list, the update is rejected.
    """
    for key, raw_value in updates:
        if allowed_keys and key not in allowed_keys:
            log.warning("Rejected config update for unknown key", extra={"key": key})
            continue
        try:
            parsed = json.loads(raw_value)
            if validators and key in validators:
                if parsed not in validators[key]:
                    log.warning(
                        "Rejected config update: value not allowed",
                        extra={"key": key, "value": parsed, "allowed": validators[key]},
                    )
                    continue
            await kv.put(key, json.dumps(parsed).encode())
            log.info("Config self-tuned", extra={"key": key, "value": parsed})
        except Exception:
            log.warning("Failed to parse config update", extra={"key": key, "raw_value": raw_value})
