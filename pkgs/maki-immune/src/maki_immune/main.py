"""maki-immune: Independent ops intelligence for system health.

Monitors all Maki components, reasons about problems via its own Claude instance,
takes autonomous reflexive actions (pod restarts), and reports to #maki-vitals.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from functools import partial

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from maki_common import (
    configure_logging,
    connect_nats,
    default_health_endpoints,
    init_kv,
    load_kv_config,
    spawn_background,
)
from maki_common.health import tcp_health_server
from maki_common.settings import (
    NATS_TOKEN,
    NATS_URL,
    RECALL_URL,
    REPO_NAME,
    REPO_OWNER,
    REPO_PATH,
)
from maki_common.subjects import (
    CORTEX_STUCK,
    DEPLOY_PROPAGATE,
    DEPLOY_REQUEST,
    DEPLOY_STATUS_REQUEST,
    EARS_IMMUNE_OUT,
    EARS_VITALS_OUT,
    IMMUNE_ALERT,
    IMMUNE_COMMAND,
    IMMUNE_LOGS_REQUEST,
    IMMUNE_SITE_QUERY,
    IMMUNE_STATE_REQUEST,
    RESTART_PROPAGATE,
    RESTART_REQUEST,
)

from maki_immune import claude as claude_mod
from maki_immune import deploy as deploy_mod
from maki_immune import health as health_mod
from maki_immune.lock import infra_lock as _infra_lock_impl

configure_logging()
log = logging.getLogger(__name__)

# --- Config ---

HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
NAMESPACE = os.environ.get("NAMESPACE", "maki")
GHCR_PREFIX = os.environ.get("GHCR_PREFIX", "ghcr.io/adhityaravi")

# ``maki-`` prefix is required here: immune's HTTP verdicts are merged with
# k8s pod verdicts (keyed on ``app=`` labels, which are ``maki-*``) inside
# ``_merge_and_update_health``. Bare-name keys would produce ghost entries
# in ``_component_health`` and prevent the composite ``http_ok AND k8s_ok``
# verdict from firing. Ports + env-var overrides come from the shared table
# in ``maki_common.endpoints`` — see #137.
HEALTH_ENDPOINTS = default_health_endpoints(prefix="maki-")

VITALS_STREAM = "maki-vitals"
DEPLOY_STREAM = "maki-deploy"
RESTART_STREAM = "maki-restart"
CONFIG_BUCKET = "maki-immune-config"
CORTEX_CONFIG_BUCKET = "maki-cortex-config"
LOCK_BUCKET = "maki-lock"
DEPLOY_HISTORY_BUCKET = "maki-deploy-history"
STATE_BUCKET = "maki-immune-state"
RECENT_ACTIONS_KEY = "recent_actions"
RECENT_ACTIONS_MAX = 100
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))
INSTANCE_ID = f"immune-{uuid.uuid4().hex[:8]}"
SITE_NAME = os.environ.get("SITE_NAME", "unknown")
GOSSIP_STALE_THRESHOLD = CHECK_INTERVAL * 3

DEFAULT_CONFIG = {
    "heartbeat_interval": 21600,
    "health_check_interval": 30,
    "reflex_restart_max": 3,
    "lock_ttl": 300,
    "passive_patrol_interval_seconds": 2700,
    # Stuck-component escalation (#245): how long a pod may sit unhealthy in a
    # non-Running/initializing phase with zero healthy replicas before immune
    # escalates, and how often it re-alerts while still stuck.
    "stuck_escalation_threshold_s": 600,
    "stuck_realert_interval_s": 3600,
    # Long-unhealthy re-escalation (#260): once a component has been unhealthy
    # for this many hours (in any shape, not just non-Running pods), re-fire
    # the Claude escalation. Plugs the autonomy gap where the restart-reflex's
    # single-shot escalation goes silent for days if not resolved (#259).
    "long_unhealthy_re_escalate_hours": 6,
    "long_unhealthy_realert_interval_s": 21600,
    # Tier-2 hive-guard breakthrough (#311): past this many hours of being
    # unhealthy, _check_long_unhealthy_components escalates even if a peer
    # has the component healthy. The default hive guard ("local-only, don't
    # page") is correct for hour-scale incidents but became a dead-zone at
    # week-scale: recall crashlooped silently for 13 days because hive
    # suppression + a narrow stuck-recovery allowlist left it with no path
    # back to human or autonomous attention. The breakthrough alert is
    # labelled LOCAL-ONLY-LONG-UNHEALTHY so the policy is visible.
    "long_unhealthy_local_only_escalate_hours": 72,
    # Tier-3 auto-recovery (#264, widened in #311): after this many seconds
    # stuck in a non-Running / initializing phase (or CrashLoopBackOff —
    # ``is_pod_stuck`` matches both), immune performs an automated
    # `delete pod` to break out of a multi-day wedge that reflex restarts
    # couldn't fix. Opt-in via allowlist, gated by a hive sanity check
    # (act only if a peer has the component healthy — proves the recipe
    # works). #311 widened the default from just ``maki-vault`` to also
    # cover the stateless application services (recall/cortex/stem/
    # ears/synapse) where ``delete pod`` is safe (kubelet recreates from
    # spec, no PVC concerns).
    "stuck_recovery_threshold_s": 86400,
    "stuck_recovery_cooldown_s": 21600,
    "stuck_recovery_allowlist": "maki-vault,maki-recall,maki-cortex,maki-stem,maki-ears,maki-synapse",
}

IMMUNE_CONFIG_VALIDATORS: dict[str, list] = {
    "heartbeat_interval": [21600, 43200, 86400],
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
- **query_site** (site_name) — query a remote site's immune for rich state (health, metrics, images, \
deploys, actions). Use when gossip shows a problem on another site and you need details.

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

## Component Dependencies
- **Cortex ↔ Stem coupling**: Stem holds pending turns waiting for cortex responses. \
If you restart or rollback cortex, ALWAYS restart stem too — otherwise stem will be stuck \
waiting for responses from a cortex that forgot about them. Stem's heartbeat watcher should \
self-heal, but restart it anyway to be safe.

## Loop Health

Stem runs two background loops. Their last successful run timestamps appear in your metrics \
under "Loop Heartbeats":
- **idle**: reflection loop, fires every 4h. Stale if last ran >6h ago.
- **work**: issue execution loop, fires daily at 03:00. Stale if last ran >26h ago.

If a loop is stale, escalate — it may be drifted (lock consumed outside the cron window), \
crashing silently, or starved by a stuck lock. Do not self-heal loops by restarting stem \
unless you're sure — stem restart drops in-flight turns. Alert first.

## Hive Awareness

You are not one instance — you are one immune system spanning multiple sites. Each site has its \
own pods, but you share state via the gossip ring. The "Hive State" section in your metrics shows \
every site's health.

When a component is down on one site but healthy on others:
- It's a local problem. Fix it locally. Don't panic.
- Maki can still think (cortex on other sites), still remember (recall on other sites), still listen.
- Mention the localized nature in your digest. "cortex down on inu, healthy on sushi/ramen — restarting."

When a component is down everywhere:
- This is a real emergency. Investigate aggressively. Check if it's the same root cause.
- Alert immediately.

When a site goes silent (no gossip):
- The site may be offline or network-partitioned. Note it. Don't assume the worst.

## Frequency Tuning
- [CONFIG:heartbeat_interval=21600] — patrol every 6 hours (default)
- [CONFIG:heartbeat_interval=43200] — patrol every 12 hours (quieter)
- [CONFIG:heartbeat_interval=86400] — patrol every 24 hours (quiet period)
These are the only allowed values. The reflex engine handles urgent issues mechanically — \
patrols are for holistic review, not rapid response.

## Reporting
- [DIGEST:...] — to #maki-vitals. Only when something matters.
- [ALERT:...] — urgent. You escalate reluctantly.
- [SILENT] — nothing changed, nothing notable. This is the default. Silence is your natural state.

## Rules
- If everything is fine and nothing changed → [SILENT]. Always.
- When you do report, be sparse. One sentence. The situation, what you found, what you did.
- Never paraphrase the metrics back. That's noise. Investigate or stay silent."""

# --- Global State ---

_nc = None
_js = None
_config_kv = None
_lock_kv = None
_deploy_history_kv = None
_state_kv = None
_k8s_v1 = None
_k8s_apps_v1 = None
_mcp_server = None
_component_health: dict = {}
_restart_history: dict[str, list[float]] = {}
_recent_actions: list[dict] = []
_deploy_history: dict[str, str] = {}
_pod_metrics: dict = {}
_semaphore = asyncio.Semaphore(1)
_failed_image_blacklist: set[str] = set()
_hive_state: dict[str, dict] = {}
_running_images: dict[str, str] = {}
_cortex_state: dict = {
    "last_heartbeat": 0,
    "active_turn": None,
    "turn_mode": None,
    "turn_started": None,
}

# Health-check inputs — populated as startup progresses. The /health endpoint
# returns 503 until NATS is up, the infra-lock KV is initialised and the
# health-monitor scan loop is running. Without this, kubelet readiness was
# meaningless: a totally broken immune was still routed traffic.
_health_monitor_task: asyncio.Task | None = None
# Critical listener tasks. They're now wrapped in ``subscribe_supervised`` so
# they should never exit cleanly on their own — if any of them is ``done()``
# the readiness probe must flip red so kubelet restarts the pod (issue #175).
_critical_listener_tasks: dict[str, asyncio.Task] = {}


def _liveness_check() -> tuple[bool, str | None]:
    """Return (ok, reason) for the ``/live`` liveness probe.

    Only restart-worthy conditions fail here: the health-monitor task or a
    supervised critical listener has exited. NATS disconnection is *not*
    liveness — it either self-heals via reconnect or belongs to readiness'
    "don't route to me" side. See issue #373 for the split rationale.
    """
    if _health_monitor_task is not None and _health_monitor_task.done():
        if _health_monitor_task.cancelled():
            return False, "Health-monitor task cancelled"
        exc = _health_monitor_task.exception()
        return False, f"Health-monitor task crashed: {exc!r}"
    for label, task in _critical_listener_tasks.items():
        if task.done():
            if task.cancelled():
                return False, f"{label} listener cancelled"
            exc = task.exception()
            return False, f"{label} listener crashed: {exc!r}"
    return True, None


def _readiness_check() -> tuple[bool, str | None]:
    """Return (ok, reason) for the ``/health`` readiness probe.

    Includes everything liveness checks plus startup/connectivity gates —
    NATS, lock KV, health-monitor task presence — that don't warrant a
    restart but should keep traffic off until they resolve.
    """
    if _nc is None or not _nc.is_connected:
        return False, "NATS not connected"
    if _lock_kv is None:
        return False, "Infrastructure-lock KV not initialised"
    if _health_monitor_task is None:
        return False, "Health-monitor task not started"
    return _liveness_check()


# Legacy alias — semantically identical to readiness, matching the
# pre-split behaviour of the single ``_health_check`` callable.
_health_check = _readiness_check


# --- Infrastructure Lock ---


async def _acquire_lock(holder: str, ttl: int = 300) -> bool:
    """Acquire infrastructure lock with server-side TTL."""
    lock_key = f"infrastructure.{SITE_NAME}"
    try:
        try:
            entry = await _lock_kv.get(lock_key)
            lock_data = json.loads(entry.value.decode())
            if time.time() - lock_data["acquired_at"] < lock_data["ttl"]:
                log.info("Lock held, cannot acquire", extra={"holder": lock_data["holder"], "site": SITE_NAME})
                return False
            log.info("Lock expired, acquiring", extra={"previous_holder": lock_data["holder"]})
        except Exception:
            pass

        lock_data = {"holder": holder, "acquired_at": time.time(), "ttl": ttl, "site": SITE_NAME}
        payload = json.dumps(lock_data).encode()
        try:
            await _lock_kv.delete(lock_key)
        except Exception:
            pass
        await _lock_kv.create(lock_key, payload, msg_ttl=ttl + 60)
        log.info("Lock acquired", extra={"holder": holder, "site": SITE_NAME, "ttl": ttl, "server_ttl": ttl + 60})
        return True
    except Exception:
        log.exception("Failed to acquire lock")
        return False


async def _release_lock(holder: str):
    """Release infrastructure lock if held by this holder."""
    lock_key = f"infrastructure.{SITE_NAME}"
    try:
        entry = await _lock_kv.get(lock_key)
        lock_data = json.loads(entry.value.decode())
        if lock_data["holder"] == holder:
            await _lock_kv.delete(lock_key)
            log.info("Lock released", extra={"holder": holder, "site": SITE_NAME})
        else:
            log.warning(
                "Lock held by different holder", extra={"current_holder": lock_data["holder"], "requested_by": holder}
            )
    except Exception:
        pass


# ``async with _infra_lock("immune-X", ttl=Y)`` for deploy/health call sites.
# Raises ``LockNotAcquired`` when the lock is held elsewhere; always releases on
# exit. Callers use ``try/except LockNotAcquired`` to render their site-specific
# "lock held" response (see #127). ``partial`` binds the module-level
# acquire/release functions so submodules don't need to know they exist — the
# CM they receive already carries the correct kv-backed implementation.
_infra_lock = partial(_infra_lock_impl, acquire=_acquire_lock, release=_release_lock)


# --- Recent Actions Persistence ---


async def _load_recent_actions():
    """Load recent_actions from KV on startup."""
    global _recent_actions
    try:
        entry = await _state_kv.get(RECENT_ACTIONS_KEY)
        loaded = json.loads(entry.value.decode())
        if isinstance(loaded, list):
            _recent_actions = loaded[-RECENT_ACTIONS_MAX:]
            log.info("Recent actions loaded from KV", extra={"entries": len(_recent_actions)})
    except Exception:
        log.info("No recent actions found in KV (first run)")


async def _persist_recent_actions():
    """Persist current _recent_actions list to KV."""
    try:
        await _state_kv.put(RECENT_ACTIONS_KEY, json.dumps(_recent_actions[-RECENT_ACTIONS_MAX:], default=str).encode())
    except Exception:
        log.warning("Failed to persist recent_actions to KV")


def _schedule_persist_recent_actions():
    """Schedule _persist_recent_actions as a background task.

    Uses ``spawn_background`` (not bare ``ensure_future``) so the task is
    anchored against mid-flight GC and any uncaught exception is logged
    instead of vanishing silently (issue #123).
    """
    spawn_background(_persist_recent_actions(), name="immune.persist_recent_actions")


# --- NATS Publishing ---


async def _publish_alert(alert_text: str):
    """Publish urgent alert to JetStream."""
    payload = {"alert": alert_text, "timestamp": time.time()}
    await _js.publish(IMMUNE_ALERT, json.dumps(payload).encode())
    log.info("Alert published", extra={"alert_preview": alert_text[:100]})


async def _publish_vitals(digest: str):
    """Publish health digest to JetStream for #maki-vitals."""
    payload = {"digest": digest, "timestamp": time.time()}
    await _js.publish(EARS_VITALS_OUT, json.dumps(payload).encode())
    log.info("Vitals digest published", extra={"digest_len": len(digest)})


async def _publish_immune_response(message_id: str, response: str):
    """Publish immune command response back to ears for #maki-immune."""
    payload = {"message_id": message_id, "response": response}
    await _nc.publish(EARS_IMMUNE_OUT, json.dumps(payload).encode())
    log.info("Immune response published", extra={"message_id": message_id, "response_len": len(response)})


# --- State Request Handlers ---


async def _state_request_handler(msg):
    """Handle NATS request for full system state (from stem/cortex)."""
    try:
        lock_info = None
        try:
            entry = await _lock_kv.get(f"infrastructure.{SITE_NAME}")
            lock_info = json.loads(entry.value.decode())
        except Exception:
            pass

        state = {
            "component_health": _component_health,
            "recent_actions": _recent_actions[-10:],
            "lock": lock_info,
            "last_cortex_heartbeat": _cortex_state["last_heartbeat"],
            "cortex_active_turn": _cortex_state["active_turn"],
            "cortex_turn_mode": _cortex_state["turn_mode"],
            "cortex_turn_started": _cortex_state["turn_started"],
            "last_incident_time": health_mod.get_last_incident_time(),
            "site_name": SITE_NAME,
            "hive_state": _hive_state,
            "images": dict(_running_images),
        }
        await msg.respond(json.dumps(state).encode())
        log.info("State request served", extra={"components": len(_component_health)})
    except Exception:
        log.exception("Failed to serve state request")
        await msg.respond(b"{}")


_LOG_REDACTION_PATTERNS: list = []


def _build_log_redaction_patterns() -> list:
    """Compile redaction regexes for pod-log responses.

    Logs can leak DB DSNs, bearer tokens, and env-style secrets. Strip the
    secret-bearing portion before handing the bytes back across NATS to cortex
    so the reflection loop never sees the raw credential (#252).
    """
    import re

    return [
        # postgres://user:password@host -> postgres://user:***@host
        (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:\s/@]+:)([^@\s]+)(@)"), r"\1***\3"),
        # Authorization: Bearer xxx / Authorization: Basic xxx
        (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)\S+"), r"\1***"),
        # Bearer/Token <value> standalone
        (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-]{12,}"), r"\1 ***"),
        # KEY=value / KEY: value for sensitive-looking env names
        (
            re.compile(
                r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|DSN))"
                r"(\s*[:=]\s*)(['\"]?)([^\s'\"]+)"
            ),
            r"\1\2\3***",
        ),
    ]


def _redact_log_text(text: str) -> str:
    """Apply the compiled redaction patterns to a log blob."""
    global _LOG_REDACTION_PATTERNS
    if not _LOG_REDACTION_PATTERNS:
        _LOG_REDACTION_PATTERNS = _build_log_redaction_patterns()
    for pattern, replacement in _LOG_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


async def _logs_request_handler(msg):
    """Serve pod logs for a component to the reflection loop (#252).

    Payload (JSON): ``{"component": str, "previous": bool, "tail_lines": int}``.
    Resolves component → pod via ``app=<component>`` label, reads logs from the
    kube-apiserver, redacts likely secrets, and replies with a JSON blob
    containing ``pod``, ``logs``, ``previous``, ``truncated`` and (on failure)
    ``error``.
    """
    try:
        try:
            payload = json.loads(msg.data.decode() or "{}")
        except Exception:
            payload = {}
        component = (payload.get("component") or "").strip()
        previous = bool(payload.get("previous", False))
        try:
            tail_lines = int(payload.get("tail_lines") or 200)
        except (TypeError, ValueError):
            tail_lines = 200
        tail_lines = max(1, min(tail_lines, 1000))

        if not component:
            await msg.respond(json.dumps({"error": "component is required"}).encode())
            return

        if _k8s_v1 is None:
            await msg.respond(json.dumps({"error": "K8s client unavailable on this immune instance"}).encode())
            return

        try:
            pods = await asyncio.to_thread(
                _k8s_v1.list_namespaced_pod,
                namespace=NAMESPACE,
                label_selector=f"app={component}",
            )
        except Exception as e:
            log.exception("logs request: pod lookup failed", extra={"component": component})
            await msg.respond(json.dumps({"error": f"pod lookup failed: {e}"}).encode())
            return

        if not pods.items:
            try:
                all_pods = await asyncio.to_thread(_k8s_v1.list_namespaced_pod, namespace=NAMESPACE)
                available = sorted(
                    {
                        p.metadata.labels.get("app")
                        for p in all_pods.items
                        if p.metadata.labels and p.metadata.labels.get("app")
                    }
                )
            except Exception:
                available = []
            await msg.respond(
                json.dumps(
                    {
                        "error": (
                            f"No pods found with app={component}. "
                            f"Available components: {', '.join(available) or '(none)'}"
                        )
                    }
                ).encode()
            )
            return

        # Prefer a Running pod for current logs; for --previous any pod with
        # a terminated previous container will do.
        def _pod_rank(p):
            phase_priority = {"Running": 0, "Pending": 1, "Failed": 2, "Succeeded": 3, "Unknown": 4}
            created_ts = p.metadata.creation_timestamp.timestamp() if p.metadata.creation_timestamp else 0
            return (phase_priority.get(p.status.phase, 5), -created_ts)

        pod = sorted(pods.items, key=_pod_rank)[0]
        pod_name = pod.metadata.name

        try:
            logs = await asyncio.to_thread(
                _k8s_v1.read_namespaced_pod_log,
                name=pod_name,
                namespace=NAMESPACE,
                tail_lines=tail_lines,
                previous=previous,
            )
        except Exception as e:
            log.warning(
                "logs request: read failed",
                extra={"component": component, "pod": pod_name, "previous": previous, "err": str(e)},
            )
            await msg.respond(
                json.dumps(
                    {
                        "pod": pod_name,
                        "component": component,
                        "previous": previous,
                        "error": f"failed to read logs: {e}",
                    }
                ).encode()
            )
            return

        logs = _redact_log_text(logs or "")
        # NATS request/reply has a default 1MB cap; keep payloads bounded.
        max_chars = 60_000
        truncated = False
        if len(logs) > max_chars:
            logs = logs[-max_chars:]
            truncated = True

        await msg.respond(
            json.dumps(
                {
                    "pod": pod_name,
                    "component": component,
                    "previous": previous,
                    "tail_lines": tail_lines,
                    "truncated": truncated,
                    "logs": logs,
                }
            ).encode()
        )
        log.info(
            "Logs request served",
            extra={
                "component": component,
                "pod": pod_name,
                "previous": previous,
                "tail_lines": tail_lines,
                "chars": len(logs),
                "truncated": truncated,
            },
        )
    except Exception:
        log.exception("Failed to serve logs request")
        try:
            await msg.respond(json.dumps({"error": "internal error"}).encode())
        except Exception:
            pass


async def _site_query_handler(msg):
    """Handle rich site query from a remote immune's Claude."""
    try:
        lock_info = None
        try:
            entry = await _lock_kv.get(f"infrastructure.{SITE_NAME}")
            lock_info = json.loads(entry.value.decode())
        except Exception:
            pass

        state = {
            "site_name": SITE_NAME,
            "instance_id": INSTANCE_ID,
            "timestamp": time.time(),
            "component_health": _component_health,
            "pod_metrics": _pod_metrics,
            "images": dict(_running_images),
            "recent_actions": _recent_actions[-20:],
            "deploy_history": _deploy_history,
            "lock": lock_info,
            "blacklist": list(_failed_image_blacklist),
            "cortex": {
                "last_heartbeat_age_s": round(time.time() - _cortex_state["last_heartbeat"], 1)
                if _cortex_state["last_heartbeat"]
                else None,
                "active_turn": _cortex_state["active_turn"],
                "turn_mode": _cortex_state["turn_mode"],
                "turn_started": _cortex_state["turn_started"],
            },
        }
        await msg.respond(json.dumps(state, default=str).encode())
        log.info("Site query served", extra={"components": len(_component_health)})
    except Exception:
        log.exception("Failed to serve site query")
        await msg.respond(b"{}")


# --- Main ---


async def main():
    global _nc, _js, _config_kv, _lock_kv, _deploy_history_kv, _state_kv, _k8s_v1, _k8s_apps_v1, _mcp_server
    global _health_monitor_task

    log.info("maki-immune starting", extra={"nats_url": NATS_URL})

    _nc = await connect_nats(NATS_URL, token=NATS_TOKEN)
    _js = _nc.jetstream()

    _config_kv = await init_kv(_js, CONFIG_BUCKET, defaults=DEFAULT_CONFIG)
    _cortex_config_kv = await init_kv(_js, CORTEX_CONFIG_BUCKET)
    _lock_kv = await init_kv(_js, LOCK_BUCKET)
    _deploy_history_kv = await init_kv(_js, DEPLOY_HISTORY_BUCKET)
    _state_kv = await init_kv(_js, STATE_BUCKET)

    # JetStream streams
    from nats.js.api import RetentionPolicy, StorageType

    for stream_name, subjects in [
        (VITALS_STREAM, [EARS_VITALS_OUT, IMMUNE_ALERT]),
        (DEPLOY_STREAM, [DEPLOY_PROPAGATE]),
        (RESTART_STREAM, [RESTART_PROPAGATE]),
    ]:
        try:
            await _js.find_stream_name_by_subject(subjects[0])
        except Exception:
            await _js.add_stream(
                name=stream_name,
                subjects=subjects,
                retention=RetentionPolicy.LIMITS,
                max_age=3600,
                storage=StorageType.FILE,
            )
            log.info("Created stream", extra={"stream": stream_name})

    # Load persistent state
    await _load_recent_actions()

    # Clone or pull the repo for local code access (read-only)
    from maki_common.repo import clean_remote_url, init_repo

    await init_repo(REPO_PATH, clone_url=clean_remote_url(REPO_OWNER, REPO_NAME))

    # K8s client
    try:
        k8s_config.load_incluster_config()
        _k8s_v1 = k8s_client.CoreV1Api()
        _k8s_apps_v1 = k8s_client.AppsV1Api()
        log.info("K8s client initialized (in-cluster)")
    except Exception:
        log.warning("K8s in-cluster config not available, pod operations disabled")

    # MCP tool server for Claude
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

    # Initialize modules
    deploy_mod.init(
        nc=_nc,
        js=_js,
        k8s_v1=_k8s_v1,
        k8s_apps_v1=_k8s_apps_v1,
        deploy_history_kv=_deploy_history_kv,
        namespace=NAMESPACE,
        instance_id=INSTANCE_ID,
        ghcr_prefix=GHCR_PREFIX,
        recent_actions=_recent_actions,
        recent_actions_max=RECENT_ACTIONS_MAX,
        deploy_history=_deploy_history,
        failed_image_blacklist=_failed_image_blacklist,
        infra_lock=_infra_lock,
        publish_alert=_publish_alert,
        publish_vitals=_publish_vitals,
        schedule_persist_recent_actions=_schedule_persist_recent_actions,
    )
    await deploy_mod.load_deploy_history()

    claude_mod.init(
        nc=_nc,
        namespace=NAMESPACE,
        instance_id=INSTANCE_ID,
        site_name=SITE_NAME,
        check_interval=CHECK_INTERVAL,
        component_health=_component_health,
        pod_metrics=_pod_metrics,
        recent_actions=_recent_actions,
        recent_actions_max=RECENT_ACTIONS_MAX,
        running_images=_running_images,
        hive_state=_hive_state,
        cortex_state=_cortex_state,
        failed_image_blacklist=_failed_image_blacklist,
        config_kv=_config_kv,
        default_config=DEFAULT_CONFIG,
        config_validators=IMMUNE_CONFIG_VALIDATORS,
        mcp_server=_mcp_server,
        semaphore=_semaphore,
        system_prompt=IMMUNE_SYSTEM_PROMPT,
        publish_alert=_publish_alert,
        publish_vitals=_publish_vitals,
        publish_immune_response=_publish_immune_response,
        schedule_persist_recent_actions=_schedule_persist_recent_actions,
        k8s_v1=_k8s_v1,
        lock_kv=_lock_kv,
    )

    health_mod.init(
        nc=_nc,
        k8s_v1=_k8s_v1,
        k8s_apps_v1=_k8s_apps_v1,
        namespace=NAMESPACE,
        instance_id=INSTANCE_ID,
        site_name=SITE_NAME,
        check_interval=CHECK_INTERVAL,
        gossip_stale_threshold=GOSSIP_STALE_THRESHOLD,
        health_endpoints=HEALTH_ENDPOINTS,
        default_config=DEFAULT_CONFIG,
        config_kv=_config_kv,
        cortex_config_kv=_cortex_config_kv,
        component_health=_component_health,
        pod_metrics=_pod_metrics,
        restart_history=_restart_history,
        recent_actions=_recent_actions,
        recent_actions_max=RECENT_ACTIONS_MAX,
        running_images=_running_images,
        hive_state=_hive_state,
        failed_image_blacklist=_failed_image_blacklist,
        cortex_state=_cortex_state,
        infra_lock=_infra_lock,
        publish_alert=_publish_alert,
        publish_vitals=_publish_vitals,
        schedule_persist_recent_actions=_schedule_persist_recent_actions,
        escalate_to_claude=claude_mod.escalate_to_claude,
    )

    # Subscriptions
    await _nc.subscribe(IMMUNE_STATE_REQUEST, queue="maki-immune", cb=_state_request_handler)
    log.info("Subscribed", extra={"subject": IMMUNE_STATE_REQUEST})

    site_query_subject = f"{IMMUNE_SITE_QUERY}.{SITE_NAME}"
    await _nc.subscribe(site_query_subject, cb=_site_query_handler)
    log.info("Subscribed", extra={"subject": site_query_subject})

    # Pod logs proxy for cortex / reflection loop (#252). Queue-grouped so any
    # immune instance in the hive can answer — they all have the same RBAC and
    # can read pods/log via the in-cluster ServiceAccount.
    await _nc.subscribe(IMMUNE_LOGS_REQUEST, queue="maki-immune", cb=_logs_request_handler)
    log.info("Subscribed", extra={"subject": IMMUNE_LOGS_REQUEST})

    await _nc.subscribe(DEPLOY_REQUEST, queue="maki-immune", cb=deploy_mod.deploy_request_handler)
    log.info("Subscribed", extra={"subject": DEPLOY_REQUEST})

    await _nc.subscribe(DEPLOY_STATUS_REQUEST, queue="maki-immune", cb=deploy_mod.deploy_status_handler)
    log.info("Subscribed", extra={"subject": DEPLOY_STATUS_REQUEST})

    await _nc.subscribe(RESTART_REQUEST, queue="maki-immune", cb=deploy_mod.restart_request_handler)
    log.info("Subscribed", extra={"subject": RESTART_REQUEST})

    await _nc.subscribe(CORTEX_STUCK, queue="maki-immune", cb=claude_mod.cortex_stuck_handler)
    log.info("Subscribed", extra={"subject": CORTEX_STUCK})

    await _nc.subscribe(IMMUNE_COMMAND, queue="maki-immune", cb=claude_mod.handle_immune_command)
    log.info("Subscribed", extra={"subject": IMMUNE_COMMAND})

    # Background tasks (tracked in ``_critical_listener_tasks`` so the
    # readiness probe flips red if any listener dies — see #175.)
    # ``spawn_background`` also anchors each task against GC and logs any
    # uncaught exception (issue #123).
    _critical_listener_tasks["deploy_propagate"] = spawn_background(
        deploy_mod.deploy_propagate_listener(), name="deploy_propagate_listener"
    )
    log.info("Started JetStream propagation listener", extra={"subject": DEPLOY_PROPAGATE})

    _critical_listener_tasks["restart_propagate"] = spawn_background(
        deploy_mod.restart_propagate_listener(), name="restart_propagate_listener"
    )
    log.info("Started JetStream restart propagation listener", extra={"subject": RESTART_PROPAGATE})

    _health_monitor_task = spawn_background(health_mod.health_monitor_loop(), name="immune.health_monitor_loop")
    spawn_background(claude_mod.immune_heartbeat_loop(), name="immune.heartbeat_loop")
    spawn_background(claude_mod.passive_log_monitor_loop(), name="immune.passive_log_monitor_loop")
    spawn_background(claude_mod.loop_heartbeat_watcher(), name="immune.loop_heartbeat_watcher")
    # Track these supervised listeners so the readiness probe can fail if any
    # of them dies — see ``_critical_listener_tasks`` and #175.
    _critical_listener_tasks["cortex_heartbeat"] = spawn_background(
        health_mod.cortex_heartbeat_listener(), name="cortex_heartbeat_listener"
    )
    _critical_listener_tasks["token_usage"] = spawn_background(
        health_mod.token_usage_listener(), name="token_usage_listener"
    )
    _critical_listener_tasks["gossip"] = spawn_background(health_mod.gossip_listener(), name="gossip_listener")
    spawn_background(health_mod.gossip_publisher(), name="immune.gossip_publisher")

    server = await tcp_health_server(
        port=HEALTH_PORT,
        checks={"/live": _liveness_check, "/health": _readiness_check},
    )
    log.info("Health server listening", extra={"port": HEALTH_PORT})

    await server.serve_forever()


def cli():
    asyncio.run(main())


if __name__ == "__main__":
    cli()
