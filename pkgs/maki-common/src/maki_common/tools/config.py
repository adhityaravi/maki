"""Config tools — read and update Maki's configuration via NATS KV."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

ALLOWED_CONFIG_KEYS = {
    "idle_interval",
    "max_thoughts_per_day",
    "idle_memory_query",
    "cortex_model",
}


def _mcp_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def make_config_tools(
    config_kv: Any,
    allowed_keys: set[str] | None = None,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for config tools."""
    effective_keys = allowed_keys or ALLOWED_CONFIG_KEYS

    async def get_config(args: dict[str, Any]) -> dict[str, Any]:
        """Read all configuration values."""
        log.info("Tool: get_config")
        try:
            keys = await config_kv.keys()
            config = {}
            for key in keys:
                entry = await config_kv.get(key)
                config[key] = entry.value.decode()
            return _mcp_result(str(config))
        except Exception as e:
            return _mcp_result(f"Failed to read config: {e}")

    async def update_config(args: dict[str, Any]) -> dict[str, Any]:
        """Update a configuration value."""
        key = args.get("key", "")
        value = args.get("value", "")
        log.info("Tool: update_config", extra={"key": key, "value": value})
        if key not in effective_keys:
            return _mcp_result(f"Key '{key}' not allowed. Allowed keys: {', '.join(sorted(effective_keys))}")
        try:
            await config_kv.put(key, value.encode())
            return _mcp_result(f"Updated {key} = {value}")
        except Exception as e:
            return _mcp_result(f"Failed to update config: {e}")

    return [
        (
            "get_config",
            "Read all current configuration values (idle interval, thought limits, etc.).",
            {},
            get_config,
        ),
        (
            "update_config",
            "Update a configuration value. Allowed keys: " + ", ".join(sorted(effective_keys)) + ".",
            {"key": str, "value": str},
            update_config,
        ),
    ]
