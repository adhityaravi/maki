"""MCP server factory — creates an in-process MCP server with all Maki tools."""

from __future__ import annotations

import json
import logging
from typing import Any

from maki_common.repo import RepoEntry, RepoRegistry

log = logging.getLogger(__name__)

# Bump: expose git push as standalone capability (tracked in issue #47)


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


def _build_repo_registry(
    repo_path: str | None,
    repo_owner: str | None,
    repo_name: str | None,
    github_auth: Any | None,
) -> RepoRegistry | None:
    """Construct a RepoRegistry seeded with the primary repo, or None."""
    if not repo_path:
        return None
    registry = RepoRegistry()
    registry.register(
        RepoEntry(
            path=repo_path,
            owner=repo_owner or "",
            name=repo_name or "",
            auth=github_auth,
        ),
        default=True,
    )
    return registry


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
    repo_owner: str | None = "adhityaravi",
    repo_name: str | None = "maki",
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
        repo_owner: GitHub owner of the primary repo (for repo registry metadata).
        repo_name: GitHub name of the primary repo (for repo registry metadata).
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

    registry = _build_repo_registry(repo_path, repo_owner, repo_name, github_auth=None)
    if registry is not None:
        from maki_common.tools.codegraph_tools import make_codegraph_tools
        from maki_common.tools.local_code import make_code_tools

        all_tools.extend(make_code_tools(registry))
        all_tools.extend(make_codegraph_tools(registry))

    # Cross-site query tool — ask a specific site's immune for rich state
    from maki_common.subjects import IMMUNE_SITE_QUERY
    from maki_common.tools.utils import mcp_result as _mcp_result

    async def _query_site(args: dict) -> dict:
        """Query a remote site's immune for detailed state."""
        site_name = args.get("site_name", "")
        if not site_name:
            return _mcp_result("site_name is required")
        subject = f"{IMMUNE_SITE_QUERY}.{site_name}"
        try:
            resp = await nc.request(subject, b"{}", timeout=10.0)
            data = json.loads(resp.data.decode())
            if not data:
                return _mcp_result(f"Site '{site_name}' returned empty response — may be unreachable.")
            return _mcp_result(json.dumps(data, indent=2, default=str))
        except Exception as e:
            return _mcp_result(f"Failed to query site '{site_name}': {e}")

    all_tools.append(
        (
            "query_site",
            "Query a remote site's immune for detailed state: component health with latency/restarts/metrics, "
            "running image tags, deploy history, recent actions, lock status, cortex state, and blacklist. "
            "Use this when gossip shows a problem on another site and you need to investigate deeper.",
            {"site_name": str},
            _query_site,
        )
    )

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
    repo_path: str | None = None,
    github_app_id: str | None = None,
    github_private_key: str | None = None,
    github_installation_id: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> Any:
    """Create an in-process MCP server with cortex tools.

    Includes: recall, health, deploy, config, local code, codegraph, github CI/issues.

    Args:
        nc: NATS client.
        recall_url: Base URL for maki-recall API.
        health_endpoints: Map of component name to health URL.
        config_kv: NATS KV store for config (optional).
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

        all_tools.extend(make_config_tools(config_kv, nc=nc))

    # Local code + CodeGraph tools (replaces GitHub API file tools)
    github_auth = None
    if github_app_id and github_private_key and github_installation_id:
        from maki_common.tools.github import GitHubAuth

        github_auth = GitHubAuth(github_app_id, github_private_key, github_installation_id)

    registry = _build_repo_registry(repo_path, repo_owner, repo_name, github_auth=github_auth)
    if registry is not None:
        from maki_common.subjects import MEMORY_STORE
        from maki_common.tools.codegraph_tools import make_codegraph_tools
        from maki_common.tools.local_code import make_code_edit_tools, make_code_tools
        from maki_common.tools.recall import MEMORY_USER_ID

        async def _on_commit_success(sha: str, message: str, repo_url: str) -> None:
            """Publish an episodic memory to NATS after every successful push."""
            url = repo_url or "unknown"
            content = f"committed and pushed {sha} to {url}: {message}"
            payload = {"content": content, "source": "cortex", "user_id": MEMORY_USER_ID}
            await nc.publish(MEMORY_STORE, json.dumps(payload).encode())
            log.info("Commit memory published", extra={"sha": sha, "repo_url": url})

        all_tools.extend(make_code_tools(registry))
        all_tools.extend(
            make_code_edit_tools(
                registry,
                on_commit_success=_on_commit_success,
            )
        )
        all_tools.extend(make_codegraph_tools(registry))

    # Discord history search (routes through ears leader via NATS request/reply)
    from maki_common.tools.discord_search import make_discord_search_tools

    all_tools.extend(make_discord_search_tools(nc))

    # Trading analysis tools (routes through stem via NATS request/reply)
    from maki_common.tools.trading_bridge import make_trading_bridge_tools

    all_tools.extend(make_trading_bridge_tools(nc))

    # Generic DB query tool (routes through stem via NATS request/reply)
    from maki_common.tools.db_bridge import make_db_query_tools

    all_tools.extend(make_db_query_tools(nc))

    # GitHub CI tools (check workflow status, logs) — needs API
    if github_app_id and github_private_key and github_installation_id and repo_owner and repo_name:
        from maki_common.tools.github import make_github_ci_tools, make_github_issues_tools

        all_tools.extend(
            make_github_ci_tools(github_app_id, github_private_key, github_installation_id, repo_owner, repo_name)
        )
        all_tools.extend(
            make_github_issues_tools(github_app_id, github_private_key, github_installation_id, repo_owner, repo_name)
        )

    sdk_tools = []
    for name, description, params, handler in all_tools:
        decorated = tool(name, description, params)(handler)
        sdk_tools.append(decorated)
        log.info("Registered cortex tool", extra={"tool": name})

    return create_sdk_mcp_server(name="maki-cortex", tools=sdk_tools)
