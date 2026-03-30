"""Health tools — check system state and component health."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)


def make_health_tools(
    nc: Any,
    health_endpoints: dict[str, str],
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for health tools."""
    from maki_common.subjects import IMMUNE_STATE_REQUEST

    async def get_system_health(args: dict[str, Any]) -> dict[str, Any]:
        """Get full system health from immune."""
        log.info("Tool: get_system_health")
        try:
            resp = await nc.request(IMMUNE_STATE_REQUEST, b"", timeout=5.0)
            return mcp_result(resp.data.decode())
        except Exception as e:
            return mcp_result(f"Failed to get system health: {e}")

    async def check_component(args: dict[str, Any]) -> dict[str, Any]:
        """Check a specific component's health endpoint."""
        name = args.get("name", "")
        log.info("Tool: check_component", extra={"component": name})
        url = health_endpoints.get(name)
        if not url:
            available = ", ".join(sorted(health_endpoints.keys()))
            return mcp_result(f"Unknown component '{name}'. Available: {available}")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url}/health")
                return mcp_result(f"{name}: status={resp.status_code}, body={resp.text}")
        except Exception as e:
            return mcp_result(f"{name}: unreachable ({e})")

    return [
        (
            "get_system_health",
            "Get detailed health status of all Maki components from the immune system. "
            "Includes restart counts, failure history, K8s pod details, and recent actions.",
            {},
            get_system_health,
        ),
        (
            "check_component",
            "Check a specific component's health endpoint directly.",
            {"name": str},
            check_component,
        ),
    ]
