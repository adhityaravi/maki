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
    recall_url: str | None = None,
    deploy_history: dict[str, str] | None = None,
    repo_path: str | None = None,
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
        recall_url: Base URL for maki-recall API (optional, enables memory tools).
        deploy_history: Mutable dict mapping deployment name to previous image (for rollbacks).
        repo_path: Local repo clone path (optional, enables code tools read-only).
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
            deploy_history=deploy_history,
        )
    )

    if recall_url:
        from maki_common.tools.recall import make_recall_tools

        all_tools.extend(make_recall_tools(recall_url, nc=nc, source="immune"))

    if config_kv is not None:
        from maki_common.tools.config import make_config_tools

        all_tools.extend(make_config_tools(config_kv, allowed_keys=IMMUNE_CONFIG_KEYS))

    if repo_path:
        from maki_common.tools.codegraph_tools import make_codegraph_tools
        from maki_common.tools.local_code import make_code_tools

        all_tools.extend(make_code_tools(repo_path))
        all_tools.extend(make_codegraph_tools(repo_path))

    sdk_tools = []
    for name, description, params, handler in all_tools:
        decorated = tool(name, description, params)(handler)
        sdk_tools.append(decorated)
        log.info("Registered immune tool", extra={"tool": name})

    return create_sdk_mcp_server(name="maki-immune", tools=sdk_tools)


def create_cortex_tools(
    nc: Any,
    recall_url: str,
    health_endpoints: dict[str, str],
    config_kv: Any | None = None,
    todo_kv: Any | None = None,
    repo_path: str | None = None,
    github_app_id: str | None = None,
    github_private_key: str | None = None,
    github_installation_id: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> Any:
    """Create an in-process MCP server with cortex tools.

    Includes: recall, health, deploy, config, todo, local code, codegraph, github CI.

    Args:
        nc: NATS client.
        recall_url: Base URL for maki-recall API.
        health_endpoints: Map of component name to health URL.
        config_kv: NATS KV store for config (optional).
        todo_kv: NATS KV store for todo list (optional).
        repo_path: Local repo clone path (optional, enables code tools).
        github_app_id: GitHub App ID (optional, enables GitHub CI tools).
        github_private_key: GitHub App private key PEM string.
        github_installation_id: GitHub App installation ID.
        repo_owner: GitHub repo owner.
        repo_name: GitHub repo name.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from maki_common.tools.deploy import make_deploy_tools
    from maki_common.tools.health import make_health_tools
    from maki_common.tools.recall import make_recall_tools

    all_tools = []
    all_tools.extend(make_recall_tools(recall_url, nc=nc, source="cortex"))
    all_tools.extend(make_health_tools(nc, health_endpoints))
    all_tools.extend(make_deploy_tools(nc))

    if config_kv is not None:
        from maki_common.tools.config import make_config_tools

        all_tools.extend(make_config_tools(config_kv))

    if todo_kv is not None:
        from maki_common.tools.todo import make_todo_tools

        all_tools.extend(make_todo_tools(todo_kv))

    # Local code + CodeGraph tools (replaces GitHub API file tools)
    github_auth = None
    if github_app_id and github_private_key and github_installation_id:
        from maki_common.tools.github import GitHubAuth

        github_auth = GitHubAuth(github_app_id, github_private_key, github_installation_id)

    if repo_path:
        from maki_common.tools.codegraph_tools import make_codegraph_tools
        from maki_common.tools.local_code import make_code_edit_tools, make_code_tools

        all_tools.extend(make_code_tools(repo_path))
        all_tools.extend(
            make_code_edit_tools(
                repo_path,
                github_auth=github_auth,
                repo_owner=repo_owner or "",
                repo_name=repo_name or "",
            )
        )
        all_tools.extend(make_codegraph_tools(repo_path))

    # GitHub CI tools (trigger builds, check status) — still needs API
    if github_app_id and github_private_key and github_installation_id and repo_owner and repo_name:
        from maki_common.tools.github import make_github_ci_tools

        all_tools.extend(
            make_github_ci_tools(github_app_id, github_private_key, github_installation_id, repo_owner, repo_name)
        )

    sdk_tools = []
    for name, description, params, handler in all_tools:
        decorated = tool(name, description, params)(handler)
        sdk_tools.append(decorated)
        log.info("Registered cortex tool", extra={"tool": name})

    return create_sdk_mcp_server(name="maki-cortex", tools=sdk_tools)
