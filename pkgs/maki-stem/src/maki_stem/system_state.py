"""System-state gather/format helpers.

Pulls rich component health from ``maki-immune`` via NATS request/reply and
falls back to plain HTTP ``/health`` probes when immune is unavailable.
Used by the interactive turn path to give cortex situational awareness of
the fleet without every turn shipping a 40-key JSON blob — the compact
one-line summary is the default; the full dict is only injected when the
user's prompt sounds health-shaped.
"""

from __future__ import annotations

import json
import logging

import httpx
from maki_common.subjects import IMMUNE_STATE_REQUEST

log = logging.getLogger(__name__)

HEALTH_KEYWORDS = frozenset(
    [
        "health",
        "status",
        "system",
        "component",
        "running",
        "restart",
        "down",
        "broken",
        "error",
        "crash",
        "fail",
        "deploy",
        "pod",
        "service",
        "immune",
        "stem",
        "cortex",
        "recall",
        "synapse",
        "nerve",
        "embed",
    ]
)


def is_health_query(message: str) -> bool:
    """Return True if the message is asking about system health/status."""
    lower = message.lower()
    return any(kw in lower for kw in HEALTH_KEYWORDS)


async def gather_system_state(
    nc,
    *,
    conversation_history_size: int,
    health_endpoints: dict[str, str],
) -> dict:
    """Gather infrastructure state for cortex self-awareness.

    Requests rich data from maki-immune via NATS request/reply, falling
    back to basic HTTP health checks when immune is unreachable.
    """
    state: dict = {
        "nats": {"connected": nc.is_connected if nc else False},
        "conversation_stream": {"total_turns": conversation_history_size},
    }

    # Try to get rich state from immune via NATS request/reply
    try:
        resp = await nc.request(IMMUNE_STATE_REQUEST, b"", timeout=2.0)
        immune_data = json.loads(resp.data.decode())
        # Merge immune's rich component health into state
        for name, info in immune_data.get("component_health", {}).items():
            state[name] = info
        if immune_data.get("recent_actions"):
            state["recent_reflex_actions"] = {"count": len(immune_data["recent_actions"])}
        log.info("Rich system state from immune", extra={"components": len(state)})
        return state
    except Exception:
        log.info("Immune state unavailable, falling back to HTTP checks")

    # Fallback: basic HTTP health checks
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, url in health_endpoints.items():
            try:
                resp = await client.get(f"{url}/health")
                state[name] = {"healthy": resp.status_code == 200}
            except Exception:
                state[name] = {"healthy": False}

    return state


def format_system_state(system_state: dict) -> str:
    """Format system state dict into readable text for memory."""
    parts = []
    for name, info in system_state.items():
        if isinstance(info, dict):
            details = ", ".join(f"{k}={v}" for k, v in info.items())
            parts.append(f"{name}: {details}")
    return "; ".join(parts) if parts else "no data"


def summarize_system_state(system_state: dict) -> str:
    """Return a one-line system health summary for non-health-focused turns."""
    problems = []
    for name, info in system_state.items():
        if not isinstance(info, dict):
            continue
        # Flag unhealthy or restarting components
        healthy = info.get("healthy", True)
        restarts = info.get("restart_count", 0) or info.get("restarts", 0)
        if not healthy:
            problems.append(f"{name}: unhealthy")
        elif restarts and int(restarts) > 3:
            problems.append(f"{name}: {restarts} restarts")
    if problems:
        return "issues: " + ", ".join(problems)
    return "all healthy"
