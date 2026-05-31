"""Health tools — check system state and component health."""

from __future__ import annotations

import json
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
    from maki_common.subjects import IMMUNE_LOGS_REQUEST, IMMUNE_STATE_REQUEST

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

    async def get_pod_logs(args: dict[str, Any]) -> dict[str, Any]:
        """Fetch live Kubernetes pod logs for a component via immune (#252).

        Args: component (str), previous (bool, default False — fetches the
        last-terminated container's logs, the kubectl ``--previous`` equivalent),
        tail_lines (int, default 200, clamped to 1000 on the immune side).
        """
        component = (args.get("component") or "").strip()
        previous_raw = args.get("previous", False)
        if isinstance(previous_raw, str):
            previous = previous_raw.strip().lower() in {"1", "true", "yes", "y"}
        else:
            previous = bool(previous_raw)
        try:
            tail_lines = int(args.get("tail_lines") or 200)
        except (TypeError, ValueError):
            tail_lines = 200
        log.info(
            "Tool: get_pod_logs",
            extra={"component": component, "previous": previous, "tail_lines": tail_lines},
        )
        if not component:
            return mcp_result("component is required (e.g. 'maki-recall', 'maki-cortex').")
        payload = json.dumps({"component": component, "previous": previous, "tail_lines": tail_lines}).encode()
        try:
            resp = await nc.request(IMMUNE_LOGS_REQUEST, payload, timeout=10.0)
        except Exception as e:
            return mcp_result(f"Failed to fetch logs for {component}: {e}")
        try:
            data = json.loads(resp.data.decode() or "{}")
        except Exception as e:
            return mcp_result(f"Failed to parse logs response: {e}")
        if not data:
            return mcp_result(f"Empty response from immune for component '{component}'.")
        if "error" in data and "logs" not in data:
            suffix = f" (pod={data['pod']})" if data.get("pod") else ""
            return mcp_result(f"{component}: {data['error']}{suffix}")
        header_bits = [f"pod={data.get('pod', '?')}"]
        if data.get("previous"):
            header_bits.append("previous=True")
        if data.get("truncated"):
            header_bits.append("truncated=True (tail shown)")
        header_bits.append(f"tail_lines={data.get('tail_lines', tail_lines)}")
        header = " ".join(header_bits)
        logs = data.get("logs") or "(empty)"
        return mcp_result(f"{header}\n---\n{logs}")

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
        (
            "get_pod_logs",
            "Fetch live Kubernetes pod logs for a Maki component, proxied via immune. "
            "Pass component=<deployment name> (e.g. 'maki-recall', 'maki-cortex'). "
            "Set previous=true to read the last-terminated container's logs "
            "(equivalent to `kubectl logs --previous`) — use this when a pod is "
            "CrashLoopBackOff and you need the stack trace from the prior crash. "
            "tail_lines defaults to 200 (max 1000). Likely secrets (DB passwords, "
            "bearer tokens, *_TOKEN/*_SECRET env values) are redacted before return.",
            {"component": str, "previous": bool, "tail_lines": int},
            get_pod_logs,
        ),
    ]
