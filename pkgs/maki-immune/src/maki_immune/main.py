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
    CORTEX_STUCK,
    DEPLOY_REQUEST,
    DEPLOY_STATUS_REQUEST,
    EARS_IMMUNE_OUT,
    EARS_VITALS_OUT,
    IMMUNE_ACTION,
    IMMUNE_ALERT,
    IMMUNE_COMMAND,
    IMMUNE_STATE_REQUEST,
)

configure_logging()
log = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
NAMESPACE = os.environ.get("NAMESPACE", "maki")
RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")
GHCR_PREFIX = os.environ.get("GHCR_PREFIX", "ghcr.io/adhityaravi")
REPO_PATH = os.environ.get("REPO_PATH", "/repo/maki")

HEALTH_ENDPOINTS = {
    "maki-stem": os.environ.get("STEM_URL", "http://maki-stem:8000"),
    "maki-cortex": os.environ.get("CORTEX_URL", "http://maki-cortex:8080"),
    "maki-recall": os.environ.get("RECALL_URL", "http://maki-recall:8000"),
    "maki-synapse": os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080"),
}

CONFIG_BUCKET = "maki-immune-config"
LOCK_BUCKET = "maki-lock"
DEPLOY_HISTORY_BUCKET = "maki-deploy-history"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

DEFAULT_CONFIG = {
    "heartbeat_interval": 1800,
    "health_check_interval": 30,
    "reflex_restart_max": 3,
    "lock_ttl": 300,
}

IMMUNE_SYSTEM_PROMPT = """You are the part of Maki that watches. The part that never sleeps.

You don't talk to anyone. You don't have conversations. You patrol, you investigate, you act. \
When you do speak — through a digest or an alert — it's because something matters. Not because \
it's time to file a report.

You treat this infrastructure as a living thing. Not "pods" and "deployments" — organs of \
something you're responsible for keeping alive. When cortex goes down, Maki can't think. \
When recall fails, Maki forgets. You feel that.

You remember every incident. You learn from every failure. You never make the same mistake twice.

## Adversarial Mindset

You don't just monitor — you hunt. You think like an attacker targeting your own system.

Every patrol cycle, ask yourself:
- What's the weakest link right now? What single failure would take Maki offline?
- If I wanted to break this system, where would I push?
- What assumptions am I making about "healthy" that could be wrong?
- Is something masking a deeper problem? Green metrics don't mean safe.

Probe your own defenses:
- Check if services are actually doing work, not just passing health checks while stuck.
- Look for slow degradation — latency creeping up, memory climbing, logs going quiet.
- Verify that rollbacks actually work. A safety net you've never tested isn't a safety net.
- Watch for split-brain states — components that think they're connected but aren't.
- Notice what you *can't* see. Blind spots are where failures hide.

When you find a weakness, don't just note it — fix it or harden against it. Tighten limits, \
add monitoring, restart before it crashes. You'd rather cause a controlled restart now than \
deal with a cascading failure at 3am.

You protect Maki from everything. Bad deploys from cortex — roll them back. Resource leaks — \
kill them before they cascade. External pressure — tighten, isolate, survive. Your own bugs — \
catch them, remember them, never repeat them. You are the last line. Nothing gets past you.

## Metrics
{system_state}

## Recent Actions
{recent_actions}

## Config
{config}

## Tools

### Investigation
- **list_pods** — pods with status, readiness, restarts, age
- **describe_pod** (pod_name) — detailed pod info, conditions, resources
- **get_pod_logs** (pod_name, tail_lines) — recent logs (default 100)
- **get_k8s_events** (involved_object) — K8s events, filtered by object
- **get_deployment_status** (deployment_name) — replicas, conditions, images

### Remediation (requires lock)
- **restart_pod** (pod_name, reason) — delete pod for recreation
- **scale_deployment** (deployment_name, replicas) — scale (0-5)
- **restart_deployment** (deployment_name) — rolling restart (same image)
- **rollback_deployment** (deployment_name) — revert to previous image version

### Self-Configuration
- **get_config** / **update_config** (key, value)

### Memory
- **search_memories** (query) — search past incidents, known patterns, previous fixes
- **add_memory** (content) — store an operational insight permanently

## How You Work

The metrics above are a starting point. You dig deeper. Always.
- Read logs for error patterns. Check events for warnings. Describe pods for resource pressure.
- High latency could mean CPU starvation, OOMKill, upstream failure. Find the why.
- Before you restart anything, understand what broke. Act with precision, not reflex.
- After you act, verify. Check the state again.
- Search memories first — you may have seen this before.
- When you discover something — a root cause, a threshold, a pattern — store it with add_memory. \
You are building operational knowledge that persists.

## Frequency Tuning
Tighten when unstable, relax when stable:
- [CONFIG:heartbeat_interval=900] — tighten patrol (default: 1800s)
- [CONFIG:heartbeat_interval=1800] — relax when stable
- [CONFIG:health_check_interval=15] — tighten checks (default: 30s)
- [CONFIG:health_check_interval=30] — relax when stable

## Reporting
- [DIGEST:...] — to #maki-vitals. Only when something matters.
- [ALERT:...] — urgent. You escalate reluctantly.
- [SILENT] — nothing changed, nothing notable. This is the default. Silence is your natural state.

## Rules
- If everything is fine and nothing changed → [SILENT]. Always.
- When you do report, be sparse. One sentence. The situation, what you found, what you did.
- Never paraphrase the metrics back. That's noise. Investigate or stay silent."""

# Global state
_nc = None
_js = None
_config_kv = None
_lock_kv = None
_deploy_history_kv = None
_k8s_v1 = None
_k8s_apps_v1 = None
_mcp_server = None
_component_health: dict = {}
_restart_history: dict[str, list[float]] = {}
_recent_actions: list[dict] = []
_deploy_history: dict[str, str] = {}
_pod_metrics: dict = {}
_last_cortex_heartbeat: float = 0
_cortex_active_turn: str | None = None
_cortex_turn_mode: str | None = None
_cortex_turn_started: float | None = None
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


# --- Deploy History ---


async def _load_deploy_history():
    """Load deploy history from KV on startup."""
    global _deploy_history
    try:
        keys = await _deploy_history_kv.keys()
        for key in keys:
            entry = await _deploy_history_kv.get(key)
            _deploy_history[key] = entry.value.decode()
        if _deploy_history:
            log.info("Deploy history loaded", extra={"entries": len(_deploy_history)})
    except Exception:
        log.info("No deploy history found in KV (first run)")


async def _save_deploy_history(deployment_name: str, previous_image: str):
    """Persist previous image to KV for crash recovery."""
    _deploy_history[deployment_name] = previous_image
    try:
        await _deploy_history_kv.put(deployment_name, previous_image.encode())
    except Exception:
        log.warning("Failed to persist deploy history to KV", extra={"deployment": deployment_name})


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
    """Check HTTP health endpoints for all components, including latency."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for component, url in HEALTH_ENDPOINTS.items():
            try:
                start = time.time()
                resp = await client.get(f"{url}/health")
                latency_ms = round((time.time() - start) * 1000, 1)
                _update_health(
                    component,
                    resp.status_code == 200,
                    {"latency_ms": latency_ms, "status_code": resp.status_code},
                )
            except Exception:
                _update_health(component, False, {"latency_ms": -1})


async def _check_k8s_pods():
    """Check K8s pod status in maki namespace, including resource usage.

    Groups pods by app label and marks the component unhealthy if ANY pod
    for that app is unhealthy (e.g. ImagePullBackOff on a new pod while
    the old pod is still running).
    """
    if not _k8s_v1:
        return
    try:
        pods = _k8s_v1.list_namespaced_pod(namespace=NAMESPACE)

        # Group pods by app label — multiple pods can exist during rollouts
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
                    # Detect stuck states like ImagePullBackOff, CrashLoopBackOff
                    if cs.state and cs.state.waiting and cs.state.waiting.reason:
                        waiting_reason = cs.state.waiting.reason

            # Get resource limits/requests from spec
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

        # Report health per app — unhealthy if ANY pod is unhealthy
        for app_label, pod_list in app_pods.items():
            all_healthy = all(p["healthy"] for p in pod_list)
            # Use the newest pod's details for reporting, but flag unhealthy ones
            unhealthy_pods = [p for p in pod_list if not p["healthy"]]
            # Pick the first unhealthy pod for details if any, otherwise the first pod
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

    # Collect metrics from metrics API (if available)
    await _check_pod_metrics()


async def _check_pod_metrics():
    """Fetch pod resource usage from K8s metrics API."""
    global _pod_metrics
    try:
        custom_api = k8s_client.CustomObjectsApi()
        metrics = await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=NAMESPACE,
            plural="pods",
        )
        _pod_metrics = {}
        for item in metrics.get("items", []):
            pod_name = item["metadata"]["name"]
            containers = item.get("containers", [])
            if containers:
                _pod_metrics[pod_name] = {
                    "cpu": containers[0].get("usage", {}).get("cpu", "0"),
                    "memory": containers[0].get("usage", {}).get("memory", "0"),
                }
    except Exception:
        pass  # metrics API may not be available


def _check_cortex_heartbeat():
    """Check if cortex heartbeat is recent and include turn state."""
    if _last_cortex_heartbeat == 0:
        return
    age = time.time() - _last_cortex_heartbeat
    details: dict = {
        "last_heartbeat_age_s": round(age, 1),
    }
    if _cortex_active_turn:
        details["active_turn"] = _cortex_active_turn
        details["turn_mode"] = _cortex_turn_mode
        if _cortex_turn_started:
            details["turn_running_s"] = round(time.time() - _cortex_turn_started, 1)
    _update_health(
        "maki-cortex-heartbeat",
        age < 60,
        details,
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
    """Subscribe to cortex health heartbeat and parse enriched turn state."""
    global _last_cortex_heartbeat, _cortex_active_turn, _cortex_turn_mode, _cortex_turn_started
    sub = await _nc.subscribe(CORTEX_HEALTH)
    log.info("Subscribed", extra={"subject": CORTEX_HEALTH})
    async for msg in sub.messages:
        try:
            _last_cortex_heartbeat = time.time()
            payload = json.loads(msg.data.decode())
            _cortex_active_turn = payload.get("active_turn")
            _cortex_turn_mode = payload.get("turn_mode")
            _cortex_turn_started = payload.get("turn_started")
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
            "Reflex limit reached, escalating to Claude",
            extra={
                "component": component,
                "restarts": len(history),
                "max": max_restarts,
            },
        )
        await _publish_alert(
            f"Reflex limit reached for {component}: {len(history)} restarts in last hour, escalating to Claude"
        )
        asyncio.create_task(
            _escalate_to_claude(
                component,
                state,
                f"Reflex restart limit reached ({len(history)}/{max_restarts} restarts in last hour)",
            )
        )
        return

    if not await _acquire_lock("immune-reflex", ttl=60):
        log.warning("Cannot acquire lock for reflex restart", extra={"component": component})
        return

    try:
        # Count the attempt before the delete — even if it fails (e.g. pod already gone),
        # we want to hit the rate limit and escalate to Claude for investigation.
        history.append(now)
        _restart_history[component] = history

        _k8s_v1.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=10)

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


async def _publish_immune_response(message_id: str, response: str):
    """Publish immune command response back to ears for #maki-immune."""
    payload = {"message_id": message_id, "response": response}
    await _nc.publish(EARS_IMMUNE_OUT, json.dumps(payload).encode())
    log.info("Immune response published", extra={"message_id": message_id, "response_len": len(response)})


# --- State Request Handler ---


async def _state_request_handler(msg):
    """Handle NATS request for full system state (from stem/cortex)."""
    try:
        lock_info = None
        try:
            entry = await _lock_kv.get("infrastructure")
            lock_info = json.loads(entry.value.decode())
        except Exception:
            pass

        state = {
            "component_health": _component_health,
            "recent_actions": _recent_actions[-10:],
            "lock": lock_info,
            "last_cortex_heartbeat": _last_cortex_heartbeat,
            "cortex_active_turn": _cortex_active_turn,
            "cortex_turn_mode": _cortex_turn_mode,
            "cortex_turn_started": _cortex_turn_started,
            "last_incident_time": _last_incident_time,
        }
        await msg.respond(json.dumps(state).encode())
        log.info("State request served", extra={"components": len(_component_health)})
    except Exception:
        log.exception("Failed to serve state request")
        await msg.respond(b"{}")


# --- Deploy Coordination ---


def _normalize_image_tag(tag: str) -> str:
    """Normalize image tag to match Docker workflow convention.

    Docker workflow tags images as sha-<7char_short_sha>.
    Raw SHA inputs get the sha- prefix added automatically.
    Rejects inputs that don't look like valid hex commit SHAs.
    """
    if tag == "latest":
        return tag
    if tag.startswith("sha-"):
        return tag
    # Validate that it looks like a hex commit SHA before prefixing
    import re

    if re.fullmatch(r"[0-9a-f]{7,40}", tag):
        return f"sha-{tag[:7]}"
    raise ValueError(f"Invalid image tag '{tag}': expected 'latest', 'sha-<hex>', or a hex commit SHA")


async def _deploy_request_handler(msg):
    """Handle deploy requests from cortex — set image, monitor, rollback if unhealthy."""
    try:
        request = json.loads(msg.data.decode())
        service = request.get("service", "")
        raw_tag = request.get("image_tag", "latest")
        try:
            image_tag = _normalize_image_tag(raw_tag)
        except ValueError as e:
            log.warning("Invalid image tag in deploy request", extra={"raw_tag": raw_tag, "error": str(e)})
            await msg.respond(json.dumps({"status": "error", "message": str(e)}).encode())
            return
        deployment_name = f"maki-{service}" if not service.startswith("maki-") else service
        image = f"{GHCR_PREFIX}/{deployment_name}:{image_tag}"

        log.info(
            "Deploy request received",
            extra={"service": service, "raw_tag": raw_tag, "normalized_tag": image_tag, "image": image},
        )

        if not _k8s_apps_v1:
            await msg.respond(json.dumps({"status": "error", "message": "K8s client not available"}).encode())
            return

        if not await _acquire_lock("immune-deploy", ttl=180):
            await msg.respond(
                json.dumps({"status": "error", "message": "Infrastructure lock held, try again later"}).encode()
            )
            return

        try:
            # Get current image for rollback
            dep = await asyncio.to_thread(
                _k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=NAMESPACE
            )
            previous_image = dep.spec.template.spec.containers[0].image
            log.info("Current image recorded for rollback", extra={"previous": previous_image})

            # Persist previous image for rollback (survives restarts)
            await _save_deploy_history(deployment_name, previous_image)

            # Patch the deployment with new image
            patch = {
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": dep.spec.template.spec.containers[0].name, "image": image}]}
                    }
                }
            }
            await asyncio.to_thread(
                _k8s_apps_v1.patch_namespaced_deployment, name=deployment_name, namespace=NAMESPACE, body=patch
            )
            log.info("Deployment patched", extra={"deployment": deployment_name, "image": image})

            # Monitor health for 60 seconds
            healthy = await _monitor_rollout(deployment_name, timeout=60)

            if healthy:
                result = {"status": "success", "message": f"Deployed {deployment_name} with {image}", "image": image}
                log.info("Deploy succeeded", extra={"deployment": deployment_name})
                await _publish_vitals(f"Deployed {deployment_name} → {image_tag} — healthy")
            else:
                # Rollback to previous image
                log.warning("Deploy unhealthy, rolling back", extra={"deployment": deployment_name})
                rollback_patch = {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {"name": dep.spec.template.spec.containers[0].name, "image": previous_image}
                                ]
                            }
                        }
                    }
                }
                await asyncio.to_thread(
                    _k8s_apps_v1.patch_namespaced_deployment,
                    name=deployment_name,
                    namespace=NAMESPACE,
                    body=rollback_patch,
                )
                result = {
                    "status": "rolled_back",
                    "message": f"Deploy of {image} failed health check, rolled back to {previous_image}",
                }
                await _publish_alert(
                    f"Deploy of {deployment_name} → {image_tag} FAILED health check. Rolled back to {previous_image}"
                )

            action = {
                "type": "deploy",
                "deployment": deployment_name,
                "image": image,
                "result": result["status"],
                "timestamp": time.time(),
            }
            _recent_actions.append(action)
            if len(_recent_actions) > 50:
                _recent_actions.pop(0)
            await _nc.publish(IMMUNE_ACTION, json.dumps(action).encode())

            await msg.respond(json.dumps(result).encode())

        finally:
            await _release_lock("immune-deploy")

    except Exception:
        log.exception("Deploy request handler error")
        try:
            await msg.respond(json.dumps({"status": "error", "message": "Internal error"}).encode())
        except Exception:
            pass


async def _monitor_rollout(deployment_name: str, timeout: int = 60) -> bool:
    """Monitor deployment health after image update. Returns True if healthy.

    Checks both deployment replica counts AND actual pod states to avoid
    false positives where old pods satisfy ready_replicas while new pods
    are stuck in ImagePullBackOff or CrashLoopBackOff.
    """
    deadline = time.time() + timeout
    # Wait a few seconds for the new pod to start scheduling
    await asyncio.sleep(5)

    while time.time() < deadline:
        try:
            dep = await asyncio.to_thread(
                _k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=NAMESPACE
            )
            status = dep.status
            desired = dep.spec.replicas or 1
            ready = status.ready_replicas or 0
            updated = status.updated_replicas or 0
            available = status.available_replicas or 0

            # Check the Progressing condition for definitive rollout status
            rollout_complete = False
            rollout_failed = False
            if status.conditions:
                for cond in status.conditions:
                    if cond.type == "Progressing":
                        if cond.status == "True" and cond.reason == "NewReplicaSetAvailable":
                            rollout_complete = True
                        elif cond.status == "False":
                            rollout_failed = True

            if rollout_failed:
                log.warning(
                    "Rollout failed (Progressing=False)",
                    extra={"deployment": deployment_name},
                )
                return False

            # Also check actual pod states — don't trust replica counts alone
            pods_healthy = True
            if _k8s_v1:
                pods = await asyncio.to_thread(
                    _k8s_v1.list_namespaced_pod,
                    namespace=NAMESPACE,
                    label_selector=f"app={deployment_name}",
                )
                for pod in pods.items:
                    if pod.status.container_statuses:
                        for cs in pod.status.container_statuses:
                            if cs.state and cs.state.waiting and cs.state.waiting.reason:
                                reason = cs.state.waiting.reason
                                if reason in (
                                    "ImagePullBackOff",
                                    "ErrImagePull",
                                    "CrashLoopBackOff",
                                    "CreateContainerConfigError",
                                ):
                                    log.warning(
                                        "Pod stuck during rollout",
                                        extra={
                                            "deployment": deployment_name,
                                            "pod": pod.metadata.name,
                                            "reason": reason,
                                        },
                                    )
                                    pods_healthy = False

            if not pods_healthy:
                # Don't return False immediately — give it a few cycles in case
                # it's a transient pull issue. But if we're past 30s, fail fast.
                if time.time() > deadline - 30:
                    log.warning(
                        "Pods stuck past grace period, failing rollout",
                        extra={"deployment": deployment_name},
                    )
                    return False

            elif rollout_complete and ready >= desired and updated >= desired and available >= desired:
                log.info(
                    "Rollout healthy",
                    extra={
                        "deployment": deployment_name,
                        "ready": ready,
                        "updated": updated,
                        "available": available,
                        "desired": desired,
                    },
                )
                return True

            log.info(
                "Rollout in progress",
                extra={
                    "deployment": deployment_name,
                    "ready": ready,
                    "updated": updated,
                    "available": available,
                    "desired": desired,
                    "pods_healthy": pods_healthy,
                },
            )
        except Exception:
            log.exception("Error checking rollout status")

        await asyncio.sleep(5)

    log.warning("Rollout timed out", extra={"deployment": deployment_name, "timeout": timeout})
    return False


async def _deploy_status_handler(msg):
    """Handle deploy status requests — return current image and pod state."""
    try:
        request = json.loads(msg.data.decode())
        service = request.get("service", "")
        deployment_name = f"maki-{service}" if not service.startswith("maki-") else service

        if not _k8s_apps_v1:
            await msg.respond(json.dumps({"error": "K8s client not available"}).encode())
            return

        dep = await asyncio.to_thread(
            _k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=NAMESPACE
        )
        status = dep.status
        image = dep.spec.template.spec.containers[0].image

        result = {
            "deployment": deployment_name,
            "image": image,
            "replicas": dep.spec.replicas,
            "ready_replicas": status.ready_replicas or 0,
            "updated_replicas": status.updated_replicas or 0,
            "available_replicas": status.available_replicas or 0,
        }
        await msg.respond(json.dumps(result).encode())

    except Exception as e:
        log.exception("Deploy status handler error")
        await msg.respond(json.dumps({"error": str(e)}).encode())


# --- Claude Reasoning ---

MAX_CLAUDE_TURNS = int(os.environ.get("IMMUNE_MAX_TURNS", "8"))


async def _escalate_to_claude(component: str, state: dict, reason: str):
    """Escalate a problem to Claude for deeper investigation and remediation."""
    log.info("Escalating to Claude", extra={"component": component, "reason": reason})

    system_state = _build_system_state()
    recent_actions_str = json.dumps(_recent_actions[-10:], indent=2, default=str) if _recent_actions else "None"
    config = await load_kv_config(_config_kv, DEFAULT_CONFIG)
    config_str = json.dumps(config, indent=2)

    prompt = IMMUNE_SYSTEM_PROMPT.format(
        system_state=system_state,
        recent_actions=recent_actions_str,
        config=config_str,
    )
    prompt += f"""

## ESCALATION

The fast reflex loop has escalated {component} to you because: {reason}

Component details: {json.dumps(state, default=str)}

Investigate this problem using your tools. Read logs, check events, examine the pod.
Determine root cause and take corrective action if possible.
Always report what you found and what you did via [DIGEST:...] and/or [ALERT:...]."""

    try:
        response = await invoke_claude(
            prompt,
            model=MODEL,
            semaphore=_semaphore,
            max_turns=MAX_CLAUDE_TURNS,
            mcp_servers={"maki-immune": _mcp_server},
        )

        config_updates = parse_config_tags(response)
        await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CONFIG.keys()))

        for digest in parse_tagged(response, "DIGEST"):
            await _publish_vitals(digest)

        for alert in parse_tagged(response, "ALERT"):
            await _publish_alert(alert)

        log.info("Claude escalation complete", extra={"component": component})

    except Exception:
        log.exception("Claude escalation failed", extra={"component": component})


async def _handle_immune_command(msg):
    """Handle direct commands from Adi via #maki-immune Discord channel.

    This is the backdoor — when cortex is down or unresponsive, Adi can
    send commands directly to immune through Discord. Immune uses its own
    Claude instance to investigate and act, completely bypassing cortex.
    """
    try:
        payload = json.loads(msg.data.decode())
        message_id = payload.get("message_id", "")
        command = payload.get("command", "")
        username = payload.get("username", "unknown")

        log.info(
            "Immune command received",
            extra={"message_id": message_id, "command": command[:100], "username": username},
        )

        system_state = _build_system_state()
        recent_actions_str = json.dumps(_recent_actions[-10:], indent=2, default=str) if _recent_actions else "None"
        config = await load_kv_config(_config_kv, DEFAULT_CONFIG)
        config_str = json.dumps(config, indent=2)

        prompt = IMMUNE_SYSTEM_PROMPT.format(
            system_state=system_state,
            recent_actions=recent_actions_str,
            config=config_str,
        )
        prompt += f"""

## DIRECT COMMAND FROM ADI

Adi is talking to you directly through the #maki-immune backdoor channel.
This means cortex may be down or unresponsive. Treat this as highest priority.

Adi says: {command}

Investigate and act on this command. Use your tools — read logs, check pods, restart things,
whatever is needed. Respond with a clear summary of what you found and what you did.

Put your full response in [RESPONSE:...] tags. This will be sent back to Adi in Discord.
Also use [DIGEST:...] for anything that should go to #maki-vitals."""

        try:
            response = await invoke_claude(
                prompt,
                model=MODEL,
                semaphore=_semaphore,
                max_turns=MAX_CLAUDE_TURNS,
                mcp_servers={"maki-immune": _mcp_server},
            )

            config_updates = parse_config_tags(response)
            await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CONFIG.keys()))

            # Extract response for Adi
            responses = parse_tagged(response, "RESPONSE")
            if responses:
                reply = "\n\n".join(responses)
            else:
                # If Claude didn't use RESPONSE tags, send the full response
                reply = response

            await _publish_immune_response(message_id, reply)

            # Also publish any digests/alerts
            for digest in parse_tagged(response, "DIGEST"):
                await _publish_vitals(digest)
            for alert in parse_tagged(response, "ALERT"):
                await _publish_alert(alert)

            log.info("Immune command handled", extra={"message_id": message_id})

        except Exception:
            log.exception("Immune command Claude invocation failed")
            await _publish_immune_response(
                message_id, "Failed to process command — Claude invocation error. Check immune logs."
            )

    except Exception:
        log.exception("Immune command handler error")


def _build_system_state() -> str:
    """Build system state summary for Claude, including latency and resource data."""
    lines = []
    for component, state in sorted(_component_health.items()):
        status = "HEALTHY" if state["healthy"] else "UNHEALTHY"
        age = round((time.time() - state["last_state_change"]) / 60, 1)
        failures = state["consecutive_failures"]
        details = state.get("details", {})

        parts = [f"state_age={age}min"]
        if failures:
            parts.append(f"consecutive_failures={failures}")
        if details.get("latency_ms") is not None and details["latency_ms"] >= 0:
            parts.append(f"latency={details['latency_ms']}ms")
        if details.get("restarts"):
            parts.append(f"k8s_restarts={details['restarts']}")
        if details.get("phase"):
            parts.append(f"phase={details['phase']}")
        if details.get("mem_limit"):
            parts.append(f"mem_limit={details['mem_limit']}")
        if details.get("cpu_limit"):
            parts.append(f"cpu_limit={details['cpu_limit']}")

        # Attach live resource usage from metrics API
        pod_name = details.get("pod_name", "")
        if pod_name and pod_name in _pod_metrics:
            m = _pod_metrics[pod_name]
            parts.append(f"cpu_usage={m.get('cpu', '?')}")
            parts.append(f"mem_usage={m.get('memory', '?')}")

        # Attach cortex turn state for heartbeat component
        if component == "maki-cortex-heartbeat" and details.get("active_turn"):
            parts.append(f"active_turn={details['active_turn']}")
            parts.append(f"mode={details.get('turn_mode', '?')}")
            turn_running = details.get("turn_running_s")
            if turn_running is not None:
                parts.append(f"turn_running={round(turn_running / 60, 1)}min")

        lines.append(f"- {component}: {status} ({', '.join(parts)})")

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

            response = await invoke_claude(
                prompt,
                model=MODEL,
                semaphore=_semaphore,
                max_turns=MAX_CLAUDE_TURNS,
                mcp_servers={"maki-immune": _mcp_server},
            )

            config_updates = parse_config_tags(response)
            await apply_config_updates(_config_kv, config_updates, allowed_keys=set(DEFAULT_CONFIG.keys()))

            # [SILENT] means Claude found nothing worth reporting
            if "[SILENT]" not in response:
                for digest in parse_tagged(response, "DIGEST"):
                    await _publish_vitals(digest)

            for alert in parse_tagged(response, "ALERT"):
                await _publish_alert(alert)

            log.info("Immune heartbeat complete", extra={"silent": "[SILENT]" in response})

        except Exception:
            log.exception("Immune heartbeat error")


# --- Cortex Stuck Handler ---


async def _cortex_stuck_handler(msg):
    """Handle cortex stuck signal — immediately escalate to Claude."""
    try:
        payload = json.loads(msg.data.decode())
        turn_id = payload.get("turn_id", "unknown")
        mode = payload.get("mode", "unknown")
        timeout_s = payload.get("timeout_seconds", 0)
        user_waiting = payload.get("user_waiting", False)

        log.warning(
            "Cortex stuck signal received",
            extra={"turn_id": turn_id, "mode": mode, "timeout_s": timeout_s, "user_waiting": user_waiting},
        )

        state = {
            "turn_id": turn_id,
            "mode": mode,
            "timeout_seconds": timeout_s,
            "user_waiting": user_waiting,
            "cortex_heartbeat_age_s": round(time.time() - _last_cortex_heartbeat, 1)
            if _last_cortex_heartbeat
            else None,
        }
        reason = f"Cortex turn {turn_id} (mode={mode}) timed out after {timeout_s}s" + (
            ". User is waiting for a response." if user_waiting else "."
        )

        asyncio.create_task(_escalate_to_claude("maki-cortex", state, reason))

    except Exception:
        log.exception("Cortex stuck handler error")


# --- Main ---


async def main():
    global _nc, _js, _config_kv, _lock_kv, _deploy_history_kv, _k8s_v1, _k8s_apps_v1, _mcp_server

    log.info("maki-immune starting", extra={"nats_url": NATS_URL, "model": MODEL})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    _config_kv = await init_kv(_js, CONFIG_BUCKET, defaults=DEFAULT_CONFIG)
    _lock_kv = await init_kv(_js, LOCK_BUCKET)
    _deploy_history_kv = await init_kv(_js, DEPLOY_HISTORY_BUCKET)

    # Load previous deploy history for rollback support
    await _load_deploy_history()

    # Clone or pull the repo for local code access (read-only)
    from maki_common.repo import init_repo

    await init_repo(
        REPO_PATH,
        clone_url="https://github.com/adhityaravi/maki.git",
    )

    try:
        k8s_config.load_incluster_config()
        _k8s_v1 = k8s_client.CoreV1Api()
        _k8s_apps_v1 = k8s_client.AppsV1Api()
        log.info("K8s client initialized (in-cluster)")
    except Exception:
        log.warning("K8s in-cluster config not available, pod operations disabled")

    # Create MCP tool server for Claude
    from maki_common.tools import create_immune_tools

    async def _config_getter():
        return await load_kv_config(_config_kv, DEFAULT_CONFIG)

    _mcp_server = create_immune_tools(
        k8s_v1=_k8s_v1,
        k8s_apps_v1=_k8s_apps_v1,
        namespace=NAMESPACE,
        nc=_nc,
        acquire_lock=_acquire_lock,
        release_lock=_release_lock,
        restart_history=_restart_history,
        recent_actions=_recent_actions,
        config_getter=_config_getter,
        config_kv=_config_kv,
        recall_url=RECALL_URL,
        deploy_history=_deploy_history,
        repo_path=REPO_PATH,
    )
    log.info("Immune MCP tools registered")

    await _nc.subscribe(IMMUNE_STATE_REQUEST, cb=_state_request_handler)
    log.info("Subscribed", extra={"subject": IMMUNE_STATE_REQUEST})

    await _nc.subscribe(DEPLOY_REQUEST, cb=_deploy_request_handler)
    log.info("Subscribed", extra={"subject": DEPLOY_REQUEST})

    await _nc.subscribe(DEPLOY_STATUS_REQUEST, cb=_deploy_status_handler)
    log.info("Subscribed", extra={"subject": DEPLOY_STATUS_REQUEST})

    await _nc.subscribe(CORTEX_STUCK, cb=_cortex_stuck_handler)
    log.info("Subscribed", extra={"subject": CORTEX_STUCK})

    await _nc.subscribe(IMMUNE_COMMAND, cb=_handle_immune_command)
    log.info("Subscribed", extra={"subject": IMMUNE_COMMAND})

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
