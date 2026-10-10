"""Health tools — check system state and component health."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)


def _name_variants(name: str) -> tuple[str, str]:
    """Return (bare, prefixed) name variants.

    The three drifting copies of ``HEALTH_ENDPOINTS`` use different conventions
    (cortex/stem use bare ``"recall"``, immune uses ``"maki-recall"``) and
    immune's ``component_health`` keys come from k8s ``app=`` labels which are
    always ``maki-*``. Accept either form from the caller and try both against
    each registry — see #265.
    """
    bare = name[len("maki-") :] if name.startswith("maki-") else name
    prefixed = name if name.startswith("maki-") else f"maki-{name}"
    return bare, prefixed


def _lookup(registry: dict[str, Any], name: str) -> Any:
    """Best-effort lookup that tolerates ``maki-`` prefix mismatch."""
    if name in registry:
        return registry[name]
    bare, prefixed = _name_variants(name)
    if prefixed in registry:
        return registry[prefixed]
    if bare in registry:
        return registry[bare]
    return None


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
        """Check a specific component's health.

        Sources, in priority order:

        1. **Immune's view** (always queried) — covers every component
           ``component_health`` knows about, which is the k8s pod scan plus the
           HTTP endpoints immune itself probes. That's how ``maki-vault``,
           ``maki-ears``, ``maki-embed``, ``maki-immune`` and
           anything else with an ``app=maki-*`` label become visible to this
           tool — the local ``health_endpoints`` registry only has four names
           and was a self-diagnosis blind spot (#265).
        2. **Local HTTP probe** — if the caller's process happens to have a URL
           for this component in its ``HEALTH_ENDPOINTS`` dict, we hit
           ``/health`` directly for a fresh single-shot reading. Useful when
           you want to compare immune's monitor-loop verdict against the
           component's own current answer.

        Return states (see #754 / #657 for the three-state disambiguation):

        - **HEALTHY / UNHEALTHY** — component is in ``component_health``, we
          know what it's doing right now.
        - **KNOWN_BUT_MISSING_FROM_CLUSTER** — immune has seen this component
          before (it's in the sticky known-components registry) but it's
          absent from the current pod/probe scan. The Deployment/Service/pods
          appear to be gone from the cluster; downstream consumers will see
          NXDOMAIN. Distinct from UNHEALTHY (probe failing) and UNKNOWN
          (name never observed).
        - **UNKNOWN** — no immune entry, no local endpoint, and not in the
          known-components registry either. Either a typo, a name that was
          never a Maki component, or the immune query itself failed.
        """
        name = (args.get("name") or "").strip()
        log.info("Tool: check_component", extra={"component": name})
        if not name:
            return mcp_result("name is required (e.g. 'maki-vault', 'recall', 'cortex').")

        # 1) Immune's view — the source of truth for the full component set.
        immune_line: str
        immune_state: dict[str, Any] | None = None
        try:
            resp = await nc.request(IMMUNE_STATE_REQUEST, b"", timeout=5.0)
            immune_state = json.loads(resp.data.decode() or "{}")
        except Exception as e:
            immune_line = f"immune view: failed to query ({e})"

        component_info: dict[str, Any] | None = None
        known_entry: dict[str, Any] | None = None
        missing_entry: dict[str, Any] | None = None
        if immune_state is not None:
            component_health = immune_state.get("component_health") or {}
            component_info = _lookup(component_health, name)
            known_components = immune_state.get("known_components") or {}
            missing_components = immune_state.get("missing_components") or {}
            known_entry = _lookup(known_components, name)
            missing_entry = _lookup(missing_components, name)

            if component_info is None:
                available = ", ".join(sorted(component_health.keys()))
                immune_line = (
                    f"immune view: '{name}' not in component_health. Known to immune: {available or '(empty)'}"
                )
            else:
                healthy = "HEALTHY" if component_info.get("healthy") else "UNHEALTHY"
                failures = component_info.get("consecutive_failures", 0)
                last_change = component_info.get("last_state_change")
                age_s = (
                    round(time.time() - last_change, 1)
                    if isinstance(last_change, (int, float)) and last_change
                    else None
                )
                details = component_info.get("details") or {}
                try:
                    details_str = json.dumps(details, default=str, sort_keys=True)
                except Exception:
                    details_str = str(details)
                immune_line = (
                    f"immune view: {healthy}, consecutive_failures={failures}, "
                    f"state_age_s={age_s}, details={details_str}"
                )

        # 2) Optional local HTTP probe — fresh single-shot reading when we
        #    happen to have an endpoint configured.
        url = _lookup(health_endpoints, name)
        http_line: str | None = None
        if url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    probe = await client.get(f"{url}/health")
                    body = probe.text[:500]
                    http_line = f"http probe ({url}/health): status={probe.status_code}, body={body}"
            except Exception as e:
                http_line = f"http probe ({url}/health): unreachable ({e})"

        # #754: KNOWN_BUT_MISSING_FROM_CLUSTER — the component's k8s
        # objects are gone. We report this ahead of the "unknown" fallthrough
        # so the caller sees the meaningful state instead of the same shape
        # as a name that never existed. Prefer the escalation tracker's
        # numbers when it's already been ticking (real "missing" duration
        # since the vanished detector engaged); fall back to the registry
        # ``last_seen_at`` when it's a fresh miss the detector hasn't scored
        # yet (< threshold ticks — see immune ``_check_missing_components``).
        if component_info is None and known_entry is not None:
            now = time.time()
            last_seen = known_entry.get("last_seen_at")
            first_seen = known_entry.get("first_seen_at")
            last_seen_age_s = round(now - last_seen, 1) if isinstance(last_seen, (int, float)) and last_seen else None
            first_seen_age_s = (
                round(now - first_seen, 1) if isinstance(first_seen, (int, float)) and first_seen else None
            )
            note_bits = [f"last_seen_age_s={last_seen_age_s}", f"first_seen_age_s={first_seen_age_s}"]
            if missing_entry is not None:
                note_bits.extend(
                    [
                        f"missing_tick_count={missing_entry.get('missing_tick_count')}",
                        f"escalation_fire_count={missing_entry.get('fire_count')}",
                    ]
                )
                first_missing = missing_entry.get("first_missing_at")
                if isinstance(first_missing, (int, float)) and first_missing:
                    note_bits.append(f"first_missing_age_s={round(now - first_missing, 1)}")
            note = ", ".join(note_bits)
            parts = [
                f"{name}: KNOWN_BUT_MISSING_FROM_CLUSTER",
                (
                    "  immune has this component in the known-components registry but its "
                    "k8s objects are absent from the current pod/probe scan — the Deployment/"
                    "Service/pods have been removed or scaled to zero. Downstream consumers "
                    "will see NXDOMAIN / connection refused."
                ),
                f"  {note}",
                f"  {immune_line}",
            ]
            if http_line is not None:
                parts.append(f"  {http_line}")
            return mcp_result("\n".join(parts))

        # If neither source produced anything actionable, say so explicitly.
        if component_info is None and http_line is None:
            local_known = ", ".join(sorted(health_endpoints.keys())) or "(none)"
            return mcp_result(
                f"{name}: UNKNOWN — no immune entry, no local /health endpoint, and "
                f"no known-components registry match.\n"
                f"  {immune_line}\n"
                f"  local endpoints: {local_known}"
            )

        parts = [f"{name}:", f"  {immune_line}"]
        if http_line is not None:
            parts.append(f"  {http_line}")
        return mcp_result("\n".join(parts))

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
            "Check a specific component's health. Always queries immune for its "
            "current view (covers every component immune sees via k8s pod scan "
            "plus HTTP probes — vault, ears, embed, immune itself, etc.) "
            "and additionally hits /health directly if this process has a local "
            "URL for the component. Accepts both bare ('vault', 'recall') and "
            "prefixed ('maki-vault', 'maki-recall') names.",
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
