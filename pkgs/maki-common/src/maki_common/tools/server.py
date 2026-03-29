"""MCP server factory — creates an in-process MCP server with all Maki tools."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def create_maki_tools(
    nc: Any,
    recall_url: str,
    health_endpoints: dict[str, str],
    config_kv: Any | None = None,
) -> Any:
    """Create an in-process MCP server with all Maki tools.

    Args:
        nc: NATS client for immune state requests.
        recall_url: Base URL for maki-recall API.
        health_endpoints: Map of component name to health URL.
        config_kv: NATS KV store for config (optional).
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from maki_common.tools.health import make_health_tools
    from maki_common.tools.recall import make_recall_tools

    all_tools = []
    all_tools.extend(make_recall_tools(recall_url))
    all_tools.extend(make_health_tools(nc, health_endpoints))

    if config_kv is not None:
        from maki_common.tools.config import make_config_tools

        all_tools.extend(make_config_tools(config_kv))

    sdk_tools = []
    for name, description, params, handler in all_tools:
        decorated = tool(name, description, params)(handler)
        sdk_tools.append(decorated)
        log.info("Registered tool", extra={"tool": name})

    return create_sdk_mcp_server(name="maki", tools=sdk_tools)


IMMUNE_CONFIG_KEYS = {
    "heartbeat_interval",
    "health_check_interval",
    "reflex_restart_max",
    "lock_ttl",
}


def create_immune_tools(
    k8s_v1: Any,
    k8s_apps_v1: Any,
    namespace: str,
    nc: Any,
    acquire_lock: Any,
    release_lock: Any,
    restart_history: dict,
    recent_actions: list,
    config_getter: Any,
    config_kv: Any | None = None,
) -> Any:
    """Create an in-process MCP server with immune-specific tools.

    Args:
        k8s_v1: Kubernetes CoreV1Api client.
        k8s_apps_v1: Kubernetes AppsV1Api client.
        namespace: K8s namespace to operate in.
        nc: NATS client for publishing actions.
        acquire_lock: Async function (holder, ttl) -> bool.
        release_lock: Async function (holder) -> None.
        restart_history: Mutable dict tracking restart times per component.
        recent_actions: Mutable list of recent actions.
        config_getter: Async callable returning current config dict.
        config_kv: NATS KV store for config (optional).
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from maki_common.tools.k8s import make_k8s_tools

    all_tools = []
    all_tools.extend(
        make_k8s_tools(
            k8s_v1,
            k8s_apps_v1,
            namespace,
            nc,
            acquire_lock,
            release_lock,
            restart_history,
            recent_actions,
            config_getter,
        )
    )

    if config_kv is not None:
        from maki_common.tools.config import make_config_tools

        all_tools.extend(make_config_tools(config_kv, allowed_keys=IMMUNE_CONFIG_KEYS))

    sdk_tools = []
    for name, description, params, handler in all_tools:
        decorated = tool(name, description, params)(handler)
        sdk_tools.append(decorated)
        log.info("Registered immune tool", extra={"tool": name})

    return create_sdk_mcp_server(name="maki-immune", tools=sdk_tools)
