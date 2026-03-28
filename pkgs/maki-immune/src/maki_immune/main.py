"""maki-immune: Independent ops intelligence for system health.

Monitors all Maki components, reasons about problems via its own Claude instance,
takes autonomous reflexive actions (pod restarts), and reports to #maki-vitals.
"""

import asyncio
import json
import logging
import os
import time

import httpx
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from maki_common import configure_logging, connect_nats, init_kv, load_kv_config, parse_config_tags
from maki_common.claude import invoke_claude
from maki_common.config import apply_config_updates, parse_tagged
from maki_common.health import tcp_health_server
from maki_common.subjects import (
    CORTEX_HEALTH,
    EARS_VITALS_OUT,
    IMMUNE_ACTION,
    IMMUNE_ALERT,
)

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
NAMESPACE = os.environ.get("NAMESPACE", "maki")

HEALTH_ENDPOINTS = {
    "maki-stem": os.environ.get("STEM_URL", "http://maki-stem:8000"),
    "maki-cortex": os.environ.get("CORTEX_URL", "http://maki-cortex:8080"),
    "maki-recall": os.environ.get("RECALL_URL", "http://maki-recall:8000"),
    "maki-synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
}

CONFIG_BUCKET = "maki-immune-config"
LOCK_BUCKET = "maki-lock"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

DEFAULT_CONFIG = {
    "heartbeat_interval": 1800,
    "health_check_interval": 30,
    "reflex_restart_max": 3,
    "lock_ttl": 300,
}

IMMUNE_SYSTEM_PROMPT = """You are maki-immune — an independent ops intelligence focused on system health and security.

You are clinical, analytical, ops-focused. Not conversational.

## Your Role
- Monitor system health holistically — look at the big picture, not just individual components
- Detect root causes, not just symptoms
- Tune your own operational parameters based on system behavior
- Report findings concisely

## Current System State
{system_state}

## Recent Reflex Actions
{recent_actions}

## Your Current Config
{config}

## Self-Tuning
You can adjust your own parameters by including tags in your response:
[CONFIG:heartbeat_interval=900] — tighten patrol frequency (seconds)
[CONFIG:health_check_interval=15] — tighten health checks (seconds)
[CONFIG:reflex_restart_max=5] — allow more autonomous restarts per hour

## Reporting
[DIGEST:your health summary here] — posted to #maki-vitals on Discord
[ALERT:urgent issue description] — urgent alert to Discord

## Instructions
Assess the system holistically. Consider:
- Are all organs healthy as a system, not just individually?
- Any cross-component correlations or cascading risks?
- Resource trends — sustainable or heading toward issues?
- If there was a recent incident, has the system stabilized?
- Should you tighten or relax your monitoring intervals?

Always include a [DIGEST:...] with a concise system status summary.
Only include [ALERT:...] for genuinely urgent issues.
Adjust config if the current intervals don't match the system's needs."""

# Global state
_nc = None
_js = None
_config_kv = None
_lock_kv = None
_k8s_v1 = None
_component_health: dict = {}
_restart_history: dict[str, list[float]] = {}
_recent_actions: list[dict] = []
_last_cortex_heartbeat: float = 0
_last_incident_time: float = 0
_semaphore = asyncio.Semaphore(1)


# --- Infrastructure Lock ---


async def _acquire_lock(holder: str, ttl: int = 300) -> bool:
    """Acquire infrastructure lock. Returns True if acquired."""
    try:
        try:
            entry = await _lock_kv.get("infrastructure")
            lock_data = json.loads(entry.value.decode())
            if time.time() - lock_data["acquired_at"] < lock_data["ttl"]:
                log.info("Lock held, cannot acquire", extra={"holder": lock_data["holder"]})
                return False
            log.info("Lock expired, acquiring", extra={"previous_holder": lock_data["holder"]})
        except Exception:
            pass

        lock_data = {"holder": holder, "acquired_at": time.time(), "ttl": ttl}
        await _lock_kv.put("infrastructure", json.dumps(lock_data).encode())
        log.info("Lock acquired", extra={"holder": holder, "ttl": ttl})
        return True
    except Exception:
        log.exception("Failed to acquire lock")
        return False


async def _release_lock(holder: str):
    """Release infrastructure lock if held by this holder."""
    try:
        entry = await _lock_kv.get("infrastructure")
        lock_data = json.loads(entry.value.decode())
        if lock_data["holder"] == holder:
            await _lock_kv.delete("infrastructure")
            log.info("Lock released", extra={"holder": holder})
        else:
            log.warning(
                "Lock held by different holder",
                extra={
                    "current_holder": lock_data["holder"],
                    "requested_by": holder,
                },
            )
    except Exception:
        pass


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


# --- Health Monitor Loop ---


async def _check_http_health():
    """Check HTTP health endpoints for all components."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for component, url in HEALTH_ENDPOINTS.items():
            try:
                resp = await client.get(f"{url}/health")
                _update_health(component, resp.status_code == 200)
            except Exception:
                _update_health(component, False)


async def _check_k8s_pods():
    """Check K8s pod status in maki namespace."""
    if not _k8s_v1:
        return
    try:
        pods = _k8s_v1.list_namespaced_pod(namespace=NAMESPACE)
        for pod in pods.items:
            app_label = pod.metadata.labels.get("app", "") if pod.metadata.labels else ""
            if not app_label:
                continue

            phase = pod.status.phase
            ready = True
            restarts = 0
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if not cs.ready:
                        ready = False
                    restarts += cs.restart_count

            healthy = phase == "Running" and ready
            _update_health(
                f"{app_label}",
                healthy,
                {
                    "phase": phase,
                    "ready": ready,
                    "restarts": restarts,
                    "pod_name": pod.metadata.name,
                },
            )
    except Exception:
        log.exception("K8s pod check failed")


def _check_cortex_heartbeat():
    """Check if cortex heartbeat is recent."""
    if _last_cortex_heartbeat == 0:
        return
    age = time.time() - _last_cortex_heartbeat
    _update_health(
        "maki-cortex-heartbeat",
        age < 60,
        {
            "last_heartbeat_age_s": round(age, 1),
        },
    )


async def _health_monitor_loop():
    """Continuous health monitoring — no Claude, triggers reflexes."""
    log.info("Health monitor loop started", extra={"interval": CHECK_INTERVAL})

    while True:
        try:
            await _check_http_health()
            await _check_k8s_pods()
            _check_cortex_heartbeat()

            config = await load_kv_config(_config_kv, DEFAULT_CONFIG)
            for component, state in _component_health.items():
                if not state["healthy"] and state["consecutive_failures"] >= 2:
                    await _trigger_reflex(component, state, config)

        except Exception:
            log.exception("Health monitor error")

        await asyncio.sleep(CHECK_INTERVAL)


# --- Cortex Heartbeat Listener ---


async def _cortex_heartbeat_listener():
    """Subscribe to cortex health heartbeat."""
    global _last_cortex_heartbeat
    sub = await _nc.subscribe(CORTEX_HEALTH)
    log.info("Subscribed", extra={"subject": CORTEX_HEALTH})
    async for msg in sub.messages:
        try:
            _last_cortex_heartbeat = time.time()
        except Exception:
            pass


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
        log.warning(
            "Reflex limit reached",
            extra={
                "component": component,
                "restarts": len(history),
                "max": max_restarts,
            },
        )
        await _publish_alert(
            f"Reflex limit reached for {component}: {len(history)} restarts in last hour, not restarting again"
        )
        return

    if not await _acquire_lock("immune-reflex", ttl=60):
        log.warning("Cannot acquire lock for reflex restart", extra={"component": component})
        return

    try:
        _k8s_v1.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=10)
        history.append(now)
        _restart_history[component] = history

        action = {
            "type": "reflex_restart",
            "component": component,
            "pod_name": pod_name,
            "restart_number": len(history),
            "max_restarts": max_restarts,
            "timestamp": now,
        }
        _recent_actions.append(action)
        if len(_recent_actions) > 50:
            _recent_actions.pop(0)

        log.info(
            "Reflex restart",
            extra={
                "component": component,
                "pod_name": pod_name,
                "restart_number": len(history),
                "max": max_restarts,
            },
        )
        await _nc.publish(IMMUNE_ACTION, json.dumps(action).encode())

    except Exception:
        log.exception("Failed to restart pod", extra={"pod_name": pod_name})
    finally:
        await _release_lock("immune-reflex")


# --- NATS Publishing ---


async def _publish_alert(alert_text: str):
    """Publish urgent alert to NATS (ears will post to #maki-vitals)."""
    payload = {"alert": alert_text, "timestamp": time.time()}
    await _nc.publish(IMMUNE_ALERT, json.dumps(payload).encode())
    log.info("Alert published", extra={"alert_preview": alert_text[:100]})


async def _publish_vitals(digest: str):
    """Publish health digest to maki-ears for #maki-vitals."""
    payload = {"digest": digest, "timestamp": time.time()}
    await _nc.publish(EARS_VITALS_OUT, json.dumps(payload).encode())
    log.info("Vitals digest published", extra={"digest_len": len(digest)})


# --- Claude Reasoning ---


def _build_system_state() -> str:
    """Build system state summary for Claude."""
    lines = []
    for component, state in sorted(_component_health.items()):
        status = "HEALTHY" if state["healthy"] else "UNHEALTHY"
        age = round((time.time() - state["last_state_change"]) / 60, 1)
        failures = state["consecutive_failures"]
        details = state.get("details", {})

        detail_str = ""
        if details.get("restarts"):
            detail_str += f", k8s_restarts={details['restarts']}"
        if details.get("phase"):
            detail_str += f", phase={details['phase']}"

        lines.append(
            f"- {component}: {status} (in this state for {age}min, consecutive_failures={failures}{detail_str})"
        )

    if not lines:
        return "No health data collected yet."
    return "\n".join(lines)


async def _immune_heartbeat_loop():
    """Periodic holistic patrol with Claude reasoning."""
    log.info("Immune heartbeat loop started")
    last_patrol = time.time()

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        try:
            config = await load_kv_config(_config_kv, DEFAULT_CONFIG)
            interval = config.get("heartbeat_interval", 1800)

            if time.time() - last_patrol < interval:
                continue

            log.info("Immune heartbeat triggered — starting patrol")
            last_patrol = time.time()

            system_state = _build_system_state()
            recent_actions_str = json.dumps(_recent_actions[-10:], indent=2) if _recent_actions else "None"
            config_str = json.dumps(config, indent=2)

            prompt = IMMUNE_SYSTEM_PROMPT.format(
                system_state=system_state,
                recent_actions=recent_actions_str,
                config=config_str,
            )

            response = await invoke_claude(prompt, model=MODEL, semaphore=_semaphore)

            config_updates = parse_config_tags(response)
            await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CONFIG.keys()))

            for digest in parse_tagged(response, "DIGEST"):
                await _publish_vitals(digest)

            for alert in parse_tagged(response, "ALERT"):
                await _publish_alert(alert)

            log.info("Immune heartbeat complete")

        except Exception:
            log.exception("Immune heartbeat error")


# --- Main ---


async def main():
    global _nc, _js, _config_kv, _lock_kv, _k8s_v1

    log.info("maki-immune starting", extra={"nats_url": NATS_URL, "model": MODEL})

    _nc = await connect_nats(NATS_URL)
    _js = _nc.jetstream()

    _config_kv = await init_kv(_js, CONFIG_BUCKET, defaults=DEFAULT_CONFIG)
    _lock_kv = await init_kv(_js, LOCK_BUCKET)

    try:
        k8s_config.load_incluster_config()
        _k8s_v1 = k8s_client.CoreV1Api()
        log.info("K8s client initialized (in-cluster)")
    except Exception:
        log.warning("K8s in-cluster config not available, pod operations disabled")

    asyncio.create_task(_health_monitor_loop())
    asyncio.create_task(_immune_heartbeat_loop())
    asyncio.create_task(_cortex_heartbeat_listener())

    server = await tcp_health_server(port=HEALTH_PORT)
    log.info("Health server listening", extra={"port": HEALTH_PORT})

    await server.serve_forever()


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
