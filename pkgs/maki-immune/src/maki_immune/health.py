"""Health monitoring, reflex engine, gossip ring, and cortex heartbeat for maki-immune."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

import httpx
from kubernetes import client as k8s_client
from maki_common import load_kv_config, subscribe_supervised
from maki_common.subjects import CORTEX_STUCK, CORTEX_TOKEN_USAGE, IMMUNE_ACTION, IMMUNE_HEALTH

# Wall-clock seconds a single cortex turn may run before immune publishes
# CORTEX_STUCK as a safety net. Cortex has its own internal asyncio.wait_for
# (CORTEX_MAX_TURN_SECONDS, default 1200s); this immune-side watchdog is the
# layer-2 catch for the case the internal timeout doesn't fire — uncancellable
# native call, asyncio loop wedged below the application layer, etc. Default
# 1800s (30 min) leaves head-room above the cortex internal default. See #150.
CORTEX_STUCK_THRESHOLD_S = int(os.environ.get("CORTEX_STUCK_THRESHOLD_S", "1800"))

log = logging.getLogger(__name__)

# Set by init()
_nc: Any = None
_k8s_v1: Any = None
_k8s_apps_v1: Any = None
_namespace: str = ""
_instance_id: str = ""
_site_name: str = ""
_check_interval: int = 30
_gossip_stale_threshold: int = 90
_health_endpoints: dict[str, str] = {}
_default_config: dict[str, Any] = {}
_config_kv: Any = None
_cortex_config_kv: Any = None  # stem's cortex config (chat_model etc.)
_component_health: Any = None
_pod_metrics: Any = None
_restart_history: Any = None
_recent_actions: Any = None
_recent_actions_max: int = 100
_running_images: Any = None
_hive_state: Any = None
_failed_image_blacklist: Any = None
_cortex_state: Any = None  # dict with last_heartbeat, active_turn, turn_mode, turn_started
_acquire_lock: Any = None
_release_lock: Any = None
_publish_alert: Any = None
_publish_vitals: Any = None
_schedule_persist: Any = None
_escalate_to_claude: Any = None  # callback, avoids circular import with claude.py
_last_incident_time: float = 0

# Token usage tracking — accumulated per day, reset on date change
_token_stats: dict[str, Any] = {
    "date": "",
    "total_tokens": 0,
    "total_cost_usd": 0.0,
    "turns": 0,
    "by_model": {},  # model -> {tokens, cost_usd, turns}
}

# Track which (turn_id, started_at) we've already published CORTEX_STUCK for so
# the watchdog doesn't re-fire every health-check tick. We re-arm on a
# ``CORTEX_STUCK_THRESHOLD_S`` cadence: if the first escalation fails to
# recover the pod (Claude path stalls, restart blocked, etc.) we keep
# alerting until either the turn clears or kubelet restarts the pod via
# the cortex liveness probe. See issue #185 — previous behaviour was
# one-shot per turn_id, which left the system silently wedged for hours.
_cortex_stuck_alerted: dict[str, Any] = {
    "turn_id": None,
    "turn_started": None,
    "last_fired_at": 0.0,
    "fire_count": 0,
}


def init(
    *,
    nc,
    k8s_v1,
    k8s_apps_v1,
    namespace,
    instance_id,
    site_name,
    check_interval,
    gossip_stale_threshold,
    health_endpoints,
    default_config,
    config_kv,
    cortex_config_kv,
    component_health,
    pod_metrics,
    restart_history,
    recent_actions,
    recent_actions_max,
    running_images,
    hive_state,
    failed_image_blacklist,
    cortex_state,
    acquire_lock,
    release_lock,
    publish_alert,
    publish_vitals,
    schedule_persist_recent_actions,
    escalate_to_claude,
):
    global _nc, _k8s_v1, _k8s_apps_v1, _namespace, _instance_id, _site_name
    global _check_interval, _gossip_stale_threshold, _health_endpoints, _default_config, _config_kv, _cortex_config_kv
    global _component_health, _pod_metrics, _restart_history, _recent_actions, _recent_actions_max
    global _running_images, _hive_state, _failed_image_blacklist, _cortex_state
    global _acquire_lock, _release_lock, _publish_alert, _publish_vitals, _schedule_persist
    global _escalate_to_claude
    _nc = nc
    _k8s_v1 = k8s_v1
    _k8s_apps_v1 = k8s_apps_v1
    _namespace = namespace
    _instance_id = instance_id
    _site_name = site_name
    _check_interval = check_interval
    _gossip_stale_threshold = gossip_stale_threshold
    _health_endpoints = health_endpoints
    _default_config = default_config
    _config_kv = config_kv
    _cortex_config_kv = cortex_config_kv
    _component_health = component_health
    _pod_metrics = pod_metrics
    _restart_history = restart_history
    _recent_actions = recent_actions
    _recent_actions_max = recent_actions_max
    _running_images = running_images
    _hive_state = hive_state
    _failed_image_blacklist = failed_image_blacklist
    _cortex_state = cortex_state
    _acquire_lock = acquire_lock
    _release_lock = release_lock
    _publish_alert = publish_alert
    _publish_vitals = publish_vitals
    _schedule_persist = schedule_persist_recent_actions
    _escalate_to_claude = escalate_to_claude


# --- Health State Tracking ---


def _update_health(component: str, healthy: bool, details: dict | None = None):
    """Update component health state and detect transitions."""
    global _last_incident_time
    now = time.time()

    if component not in _component_health:
        _component_health[component] = {
            "healthy": healthy,
            "last_check": now,
            "last_state_change": now,
            "consecutive_failures": 0 if healthy else 1,
            "details": details or {},
        }
        return

    state = _component_health[component]
    was_healthy = state["healthy"]

    if was_healthy and not healthy:
        state["last_state_change"] = now
        state["consecutive_failures"] = 1
        _last_incident_time = now
        log.warning("Component unhealthy", extra={"component": component})
    elif not was_healthy and healthy:
        state["last_state_change"] = now
        state["consecutive_failures"] = 0
        log.info("Component recovered", extra={"component": component})
    elif not healthy:
        state["consecutive_failures"] += 1

    state["healthy"] = healthy
    state["last_check"] = now
    state["details"] = details or state["details"]


def get_last_incident_time() -> float:
    return _last_incident_time


# --- Health Checks ---


async def _check_http_health():
    """Check HTTP health endpoints for all components, including latency."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for component, url in _health_endpoints.items():
            try:
                start = time.time()
                resp = await client.get(f"{url}/health")
                latency_ms = round((time.time() - start) * 1000, 1)
                _update_health(
                    component, resp.status_code == 200, {"latency_ms": latency_ms, "status_code": resp.status_code}
                )
            except Exception:
                _update_health(component, False, {"latency_ms": -1})


async def _check_k8s_pods():
    """Check K8s pod status in maki namespace, including resource usage."""
    if not _k8s_v1:
        return
    try:
        pods = await asyncio.to_thread(_k8s_v1.list_namespaced_pod, namespace=_namespace)

        app_pods: dict[str, list[dict]] = {}
        for pod in pods.items:
            app_label = pod.metadata.labels.get("app", "") if pod.metadata.labels else ""
            if not app_label:
                continue

            phase = pod.status.phase
            ready = True
            restarts = 0
            waiting_reason = None
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if not cs.ready:
                        ready = False
                    restarts += cs.restart_count
                    if cs.state and cs.state.waiting and cs.state.waiting.reason:
                        waiting_reason = cs.state.waiting.reason

            mem_limit = None
            cpu_limit = None
            container = pod.spec.containers[0] if pod.spec.containers else None
            if container and container.resources:
                limits = container.resources.limits or {}
                mem_limit = limits.get("memory")
                cpu_limit = limits.get("cpu")

            pod_info = {
                "phase": phase,
                "ready": ready,
                "restarts": restarts,
                "pod_name": pod.metadata.name,
                "mem_limit": mem_limit,
                "cpu_limit": cpu_limit,
                "waiting_reason": waiting_reason,
                "healthy": phase == "Running" and ready and waiting_reason is None,
            }

            if app_label not in app_pods:
                app_pods[app_label] = []
            app_pods[app_label].append(pod_info)

        for app_label, pod_list in app_pods.items():
            all_healthy = all(p["healthy"] for p in pod_list)
            unhealthy_pods = [p for p in pod_list if not p["healthy"]]
            report_pod = unhealthy_pods[0] if unhealthy_pods else pod_list[0]
            details = {
                "phase": report_pod["phase"],
                "ready": report_pod["ready"],
                "restarts": report_pod["restarts"],
                "pod_name": report_pod["pod_name"],
                "mem_limit": report_pod["mem_limit"],
                "cpu_limit": report_pod["cpu_limit"],
            }
            if report_pod["waiting_reason"]:
                details["waiting_reason"] = report_pod["waiting_reason"]
            if len(pod_list) > 1:
                details["total_pods"] = len(pod_list)
                details["unhealthy_pods"] = len(unhealthy_pods)

            _update_health(app_label, all_healthy, details)
    except Exception:
        log.exception("K8s pod check failed")

    await _check_pod_metrics()


async def _check_pod_metrics():
    """Fetch pod resource usage from K8s metrics API."""
    try:
        custom_api = k8s_client.CustomObjectsApi()
        metrics = await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=_namespace,
            plural="pods",
        )
        _pod_metrics.clear()
        for item in metrics.get("items", []):
            pod_name = item["metadata"]["name"]
            containers = item.get("containers", [])
            if containers:
                _pod_metrics[pod_name] = {
                    "cpu": containers[0].get("usage", {}).get("cpu", "0"),
                    "memory": containers[0].get("usage", {}).get("memory", "0"),
                }
    except Exception:
        pass


# --- Cortex Heartbeat ---


def _hive_cortex_status() -> tuple[int, int]:
    """Count how many hive sites (local + remote peers) have healthy cortex."""
    # Include local site
    total = 1
    local_last = _cortex_state["last_heartbeat"]
    healthy = 1 if local_last and (time.time() - local_last) < 60 else 0
    # Include remote peers from hive gossip
    for _site, state in _hive_state.items():
        total += 1
        cortex_info = state.get("cortex", {})
        age = cortex_info.get("last_heartbeat_age_s")
        if age is not None and age < 60:
            healthy += 1
    return total, healthy


def component_healthy_in_hive(component: str) -> list[str]:
    """Return list of hive site names where this component is healthy."""
    healthy_sites = []
    for site, state in _hive_state.items():
        peer_health = state.get("component_health", {})
        comp_state = peer_health.get(component, {})
        if comp_state.get("healthy"):
            healthy_sites.append(site)
    return healthy_sites


def _check_cortex_heartbeat():
    """Check if cortex heartbeat is recent and include turn state + hive context.

    Also acts as the layer-2 stuck-turn watchdog: if a turn has been running
    longer than ``CORTEX_STUCK_THRESHOLD_S`` we publish ``CORTEX_STUCK`` so
    the existing escalation path (``cortex_stuck_handler`` in claude.py) can
    decide what to do — restart the pod, page Adi, etc. This is the safety net
    for the case where cortex's own internal timeout failed to fire (e.g.,
    uncancellable native call). Only the active turn is tracked, and we alert
    once per (turn_id, turn_started) tuple to avoid spamming every tick.
    """
    if _cortex_state["last_heartbeat"] == 0:
        return
    now = time.time()
    age = now - _cortex_state["last_heartbeat"]
    hive_total, hive_healthy = _hive_cortex_status()
    details: dict = {
        "last_heartbeat_age_s": round(age, 1),
        "hive_cortex_total": hive_total,
        "hive_cortex_healthy": hive_healthy,
    }
    active_turn = _cortex_state["active_turn"]
    turn_mode = _cortex_state["turn_mode"]
    turn_started = _cortex_state["turn_started"]
    turn_running_s: float | None = None
    if active_turn:
        details["active_turn"] = active_turn
        details["turn_mode"] = turn_mode
        if turn_started:
            turn_running_s = now - turn_started
            details["turn_running_s"] = round(turn_running_s, 1)

    local_healthy = age < 60
    if not local_healthy and hive_healthy > 0:
        details["hive_note"] = f"cortex healthy on {hive_healthy} other site(s) — local issue only"

    _update_health("maki-cortex-heartbeat", local_healthy, details)

    # Reset stuck-alerted tracking when the active turn changes (or clears).
    if _cortex_stuck_alerted["turn_id"] != active_turn or _cortex_stuck_alerted["turn_started"] != turn_started:
        _cortex_stuck_alerted["turn_id"] = active_turn
        _cortex_stuck_alerted["turn_started"] = turn_started
        _cortex_stuck_alerted["last_fired_at"] = 0.0
        _cortex_stuck_alerted["fire_count"] = 0

    # Layer-2 watchdog: cortex turn ran past the threshold. Heartbeat must
    # still be live — if heartbeat itself is stale, the regular health-check
    # path handles the unhealthy state and a restart reflex can kick in.
    #
    # Re-arming: the previous one-shot-per-turn_id behaviour meant a single
    # failed escalation left the system silently wedged for hours (#185).
    # Now we re-fire CORTEX_STUCK on a ``CORTEX_STUCK_THRESHOLD_S`` cadence
    # until the turn clears or the pod is restarted.
    if (
        active_turn
        and turn_running_s is not None
        and turn_running_s > CORTEX_STUCK_THRESHOLD_S
        and local_healthy
        and _nc is not None
    ):
        last_fired = _cortex_stuck_alerted.get("last_fired_at") or 0.0
        time_since_last = now - last_fired
        first_fire = last_fired == 0.0
        # Fire on first crossing, then re-arm every threshold interval.
        if first_fire or time_since_last >= CORTEX_STUCK_THRESHOLD_S:
            fire_count = int(_cortex_stuck_alerted.get("fire_count") or 0) + 1
            log.error(
                "Cortex turn exceeded stuck threshold — publishing CORTEX_STUCK",
                extra={
                    "turn_id": active_turn,
                    "turn_mode": turn_mode,
                    "turn_running_s": round(turn_running_s, 1),
                    "threshold_s": CORTEX_STUCK_THRESHOLD_S,
                    "fire_count": fire_count,
                    "time_since_last_fire_s": round(time_since_last, 1) if not first_fire else None,
                },
            )
            payload = json.dumps(
                {
                    "turn_id": active_turn,
                    "mode": turn_mode or "unknown",
                    "timeout_seconds": int(turn_running_s),
                    "user_waiting": False,
                    "source": "immune_watchdog",
                    "fire_count": fire_count,
                }
            ).encode()
            # Fire-and-forget so the synchronous check doesn't block the loop.
            try:
                asyncio.create_task(_nc.publish(CORTEX_STUCK, payload))
            except Exception:
                log.exception("Failed to schedule CORTEX_STUCK publish")
            _cortex_stuck_alerted["last_fired_at"] = now
            _cortex_stuck_alerted["fire_count"] = fire_count


async def _handle_cortex_heartbeat(msg) -> None:
    _cortex_state["last_heartbeat"] = time.time()
    payload = json.loads(msg.data.decode())
    _cortex_state["active_turn"] = payload.get("active_turn")
    _cortex_state["turn_mode"] = payload.get("turn_mode")
    _cortex_state["turn_started"] = payload.get("turn_started")


async def cortex_heartbeat_listener():
    """Subscribe to cortex health heartbeat and parse enriched turn state.

    Wrapped in ``subscribe_supervised`` so a silent subscription drop or a
    NATS client close re-subscribes with backoff instead of leaving the task
    finished and ``_cortex_state["last_heartbeat"]`` frozen — which would
    flip ``maki-cortex-heartbeat`` unhealthy and could trigger a reflex
    restart on a perfectly healthy cortex (issue #175).
    """
    from maki_common.subjects import CORTEX_HEALTH

    await subscribe_supervised(
        _nc,
        CORTEX_HEALTH,
        _handle_cortex_heartbeat,
        name="cortex_heartbeat",
    )


async def _handle_token_usage(msg) -> None:
    payload = json.loads(msg.data.decode())
    today = datetime.now().strftime("%Y-%m-%d")
    if _token_stats["date"] != today:
        _token_stats["date"] = today
        _token_stats["total_tokens"] = 0
        _token_stats["total_cost_usd"] = 0.0
        _token_stats["turns"] = 0
        _token_stats["by_model"] = {}

    tokens = payload.get("total_tokens", 0)
    cost = payload.get("total_cost_usd", 0.0)
    model = payload.get("model", "unknown")

    _token_stats["total_tokens"] += tokens
    _token_stats["total_cost_usd"] += cost
    _token_stats["turns"] += 1

    if model not in _token_stats["by_model"]:
        _token_stats["by_model"][model] = {"tokens": 0, "cost_usd": 0.0, "turns": 0}
    _token_stats["by_model"][model]["tokens"] += tokens
    _token_stats["by_model"][model]["cost_usd"] += cost
    _token_stats["by_model"][model]["turns"] += 1


async def token_usage_listener():
    """Subscribe to cortex token usage and accumulate daily stats."""
    subject = f"{CORTEX_TOKEN_USAGE}.{_site_name}"
    await subscribe_supervised(
        _nc,
        subject,
        _handle_token_usage,
        name="token_usage",
    )


# --- Health Monitor Loop ---


async def health_monitor_loop():
    """Continuous health monitoring — no Claude, triggers reflexes."""
    log.info("Health monitor loop started", extra={"interval": _check_interval, "instance_id": _instance_id})

    while True:
        try:
            await _check_http_health()
            await _check_k8s_pods()
            _check_cortex_heartbeat()

            config = await load_kv_config(_config_kv, _default_config)
            for component, state in _component_health.items():
                if not state["healthy"] and state["consecutive_failures"] >= 2:
                    await _trigger_reflex(component, state, config)

        except Exception:
            log.exception("Health monitor error")

        await asyncio.sleep(_check_interval)


# --- Reflex Engine ---


async def _trigger_reflex(component: str, state: dict, config: dict):
    """Autonomous pod restart reflex (Tier 1)."""
    if component.endswith("-heartbeat"):
        return

    pod_name = state.get("details", {}).get("pod_name")
    if not pod_name:
        return

    now = time.time()
    hour_ago = now - 3600
    max_restarts = config.get("reflex_restart_max", 3)

    history = _restart_history.get(component, [])
    history = [t for t in history if t > hour_ago]
    _restart_history[component] = history

    if len(history) >= max_restarts:
        hive_healthy_elsewhere = component_healthy_in_hive(component)
        if hive_healthy_elsewhere:
            log.warning(
                "Reflex limit reached but component healthy on other sites — skipping escalation",
                extra={"component": component, "restarts": len(history), "hive_healthy_sites": hive_healthy_elsewhere},
            )
            return

        log.warning(
            "Reflex limit reached, escalating to Claude",
            extra={"component": component, "restarts": len(history), "max": max_restarts},
        )
        await _publish_alert(
            f"Reflex limit reached for {component}: {len(history)} restarts in last hour, escalating to Claude"
        )
        asyncio.create_task(
            _escalate_to_claude(
                component, state, f"Reflex restart limit reached ({len(history)}/{max_restarts} restarts in last hour)"
            )
        )
        return

    if not await _acquire_lock("immune-reflex", ttl=60):
        log.warning("Cannot acquire lock for reflex restart", extra={"component": component})
        return

    try:
        history.append(now)
        _restart_history[component] = history

        await asyncio.to_thread(
            _k8s_v1.delete_namespaced_pod,
            name=pod_name,
            namespace=_namespace,
            grace_period_seconds=10,
        )

        action = {
            "type": "reflex_restart",
            "component": component,
            "pod_name": pod_name,
            "restart_number": len(history),
            "max_restarts": max_restarts,
            "timestamp": now,
        }
        _recent_actions.append(action)
        if len(_recent_actions) > _recent_actions_max:
            _recent_actions.pop(0)
        _schedule_persist()

        log.info(
            "Reflex restart",
            extra={"component": component, "pod_name": pod_name, "restart_number": len(history), "max": max_restarts},
        )
        await _nc.publish(IMMUNE_ACTION, json.dumps(action).encode())

    except Exception:
        log.exception("Failed to restart pod", extra={"pod_name": pod_name})
    finally:
        await _release_lock("immune-reflex")


# --- Gossip Ring ---


async def _refresh_running_images():
    """Query K8s for current image tags on all maki deployments/statefulsets."""
    if not _k8s_apps_v1:
        return
    images: dict[str, str] = {}
    try:
        deps = await asyncio.to_thread(_k8s_apps_v1.list_namespaced_deployment, namespace=_namespace)
        for dep in deps.items:
            name = dep.metadata.name
            if not name.startswith("maki-") or name == "maki-nerve-nats-box":
                continue
            img = dep.spec.template.spec.containers[0].image
            images[name] = img.rsplit(":", 1)[-1] if ":" in img else "latest"
    except Exception:
        log.exception("Failed to refresh deployment images")

    try:
        sts_list = await asyncio.to_thread(_k8s_apps_v1.list_namespaced_stateful_set, namespace=_namespace)
        for sts in sts_list.items:
            name = sts.metadata.name
            if not name.startswith("maki-"):
                continue
            img = sts.spec.template.spec.containers[0].image
            if "ghcr.io" not in img:
                continue
            images[name] = img.rsplit(":", 1)[-1] if ":" in img else "latest"
    except Exception:
        log.exception("Failed to refresh statefulset images")

    if images:
        _running_images.clear()
        _running_images.update(images)


async def gossip_publisher():
    """Broadcast local state to all immune instances via NATS gossip."""
    log.info("Gossip publisher started", extra={"site": _site_name, "interval": _check_interval})
    while True:
        try:
            await _refresh_running_images()
            # Load cortex config (chat_model etc.) for gossip
            try:
                cortex_config = await load_kv_config(_cortex_config_kv, {})
            except Exception:
                cortex_config = {}

            payload = {
                "site": _site_name,
                "instance_id": _instance_id,
                "timestamp": time.time(),
                "component_health": {
                    k: {"healthy": v["healthy"], "consecutive_failures": v["consecutive_failures"]}
                    for k, v in _component_health.items()
                },
                "recent_actions": _recent_actions[-10:],
                "cortex": {
                    "last_heartbeat_age_s": round(time.time() - _cortex_state["last_heartbeat"], 1)
                    if _cortex_state["last_heartbeat"]
                    else None,
                    "active_turn": _cortex_state["active_turn"],
                    "turn_mode": _cortex_state["turn_mode"],
                },
                "cortex_config": cortex_config,
                "token_usage_today": {
                    "date": _token_stats["date"],
                    "total_tokens": _token_stats["total_tokens"],
                    "total_cost_usd": round(_token_stats["total_cost_usd"], 4),
                    "turns": _token_stats["turns"],
                    "by_model": _token_stats["by_model"],
                },
                "blacklist": list(_failed_image_blacklist),
                "images": dict(_running_images),
            }
            await _nc.publish(IMMUNE_HEALTH, json.dumps(payload).encode())
        except Exception:
            log.exception("Gossip publish failed")
        await asyncio.sleep(_check_interval)


async def _handle_gossip(msg) -> None:
    payload = json.loads(msg.data.decode())
    site = payload.get("site", "unknown")
    if site == _site_name:
        return

    was_new = site not in _hive_state
    _hive_state[site] = {**payload, "received_at": time.time()}

    if was_new:
        log.info("Peer joined hive", extra={"site": site, "instance_id": payload.get("instance_id")})

    now = time.time()
    stale = [s for s, v in _hive_state.items() if now - v["received_at"] > _gossip_stale_threshold]
    for s in stale:
        log.warning("Peer went silent, pruning", extra={"site": s})
        del _hive_state[s]


async def gossip_listener():
    """Subscribe to gossip from all immune instances, build hive-wide state."""
    await subscribe_supervised(
        _nc,
        IMMUNE_HEALTH,
        _handle_gossip,
        name="gossip",
    )
