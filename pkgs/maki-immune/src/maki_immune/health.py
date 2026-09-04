"""Health monitoring, reflex engine, gossip ring, and cortex heartbeat for maki-immune.

Replaces the pre-#102/#167 pattern of ``init()`` + module-level globals with
:class:`ImmuneHealthMonitor` — dependencies (including the
``escalate_to_claude`` callback that used to sit as a global to dodge an
imagined circular import) now enter through ``__init__`` and live on the
instance. All the per-incident trackers (``_stuck_alerted``,
``_long_unhealthy_alerted``, ``_stuck_recovery_attempts``,
``_self_health_alerted``, ``_cortex_stuck_alerted``, ``_token_stats``) are
instance attributes too, so a test suite can spin up a fresh monitor without
process-global state bleeding in.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx
from kubernetes import client as k8s_client
from maki_common import load_kv_config, spawn_background, subscribe_supervised
from maki_common.subjects import CORTEX_STUCK, CORTEX_TOKEN_USAGE, IMMUNE_ACTION, IMMUNE_HEALTH

from maki_immune.lock import LockNotAcquired

# Wall-clock seconds a single cortex turn may run before immune publishes
# CORTEX_STUCK as a safety net. Cortex has its own internal asyncio.wait_for
# (CORTEX_MAX_TURN_SECONDS, default 1200s); this immune-side watchdog is the
# layer-2 catch for the case the internal timeout doesn't fire — uncancellable
# native call, asyncio loop wedged below the application layer, etc. Default
# 1800s (30 min) leaves head-room above the cortex internal default. See #150.
CORTEX_STUCK_THRESHOLD_S = int(os.environ.get("CORTEX_STUCK_THRESHOLD_S", "1800"))

# Wall-clock seconds a component may sit unhealthy in a non-Running / initializing
# phase with zero healthy replicas before immune classifies it as "stuck" and
# escalates. This catches the gap the restart reflex misses: a pod that never
# finishes initializing (phase=Pending, waiting_reason=PodInitializing, restarts=0)
# produces no crash signal — it looks nothing like a CrashLoopBackOff — so it can
# sit dead for days while only a monotonic failure counter ticks up and nobody
# acts. See issue #245.
#
# This is deliberately distinct from #77 (suppress PodInitializing false-positives
# on healthy rolling updates): the zero-healthy-replicas guard below means we only
# escalate when NO replica is serving, so a routine multi-pod rollout — where other
# replicas stay ready — never trips this.
STUCK_ESCALATION_THRESHOLD_S = int(os.environ.get("STUCK_ESCALATION_THRESHOLD_S", "600"))

# How often to re-fire a stuck escalation while the component stays stuck. The
# first escalation fires once the threshold above is crossed; after that we
# re-alert on this cadence so a single failed/ignored escalation doesn't leave
# the system silently wedged for days (mirrors the cortex-stuck re-arm in #185),
# while staying far coarser than the 30s health tick so Adi isn't spammed.
STUCK_REALERT_INTERVAL_S = int(os.environ.get("STUCK_REALERT_INTERVAL_S", "3600"))

# Wall-clock hours a component may sit unhealthy *in any shape* (HTTP probe
# failing, CrashLoopBackOff, you name it) before immune re-fires the Claude
# escalation. This is the generalised cousin of STUCK_ESCALATION_THRESHOLD_S:
# the latter only catches non-Running/initializing pods, while this catches the
# "Running+ready=False for days" silent-hang that the restart reflex's single-
# shot escalation buries (see #259/#258 — maki-recall went 3.5 days with no
# alert because consecutive_failures kept ticking up but escalation never
# re-fired). 6h default trades "fast detection" against "don't page on a
# 30-min outage that's already being handled". See #260.
LONG_UNHEALTHY_RE_ESCALATE_HOURS = int(os.environ.get("LONG_UNHEALTHY_RE_ESCALATE_HOURS", "6"))

# Cooldown between successive re-escalations for the same component while it
# stays unhealthy. Without this, a stuck component would fire a Claude
# escalation on every 30s health tick once it crosses the threshold. Default
# matches the threshold (6h) so we get one alert per cooldown window — enough
# that it doesn't go silent for days, infrequent enough that Adi isn't spammed.
LONG_UNHEALTHY_REALERT_INTERVAL_S = int(os.environ.get("LONG_UNHEALTHY_REALERT_INTERVAL_S", "21600"))

# Tier-2 hive-guard breakthrough (#311): once a component has been unhealthy
# for *this* many hours, escalate even if some peer site has it healthy. The
# normal hive guard ("don't page about local-only issues") is correct for
# hour-scale incidents — a peer covering for us means the system as a whole
# is fine — but its implicit assumption is that *something* else will fix the
# local instance. For non-allowlisted components there is no autonomous
# recovery path, so a "local-only" outage can sit silent for weeks (recall
# was 13 days CrashLoopBackOff before this gap was closed). Past this longer
# threshold we treat the silence itself as the bug worth paging on, and the
# alert/action are tagged ``LOCAL-ONLY-LONG-UNHEALTHY`` so the policy is
# visible in the incident trail. Default 72h: well beyond any "give the
# reflex / hive a chance to fix it" window, well short of "another week of
# nothing".
LONG_UNHEALTHY_LOCAL_ONLY_ESCALATE_HOURS = int(os.environ.get("LONG_UNHEALTHY_LOCAL_ONLY_ESCALATE_HOURS", "72"))

# Wall-clock seconds a component may sit stuck in a non-Running/initializing
# phase before immune fires an automated ``delete pod`` recovery (Tier 3). See
# issue #264: maki-vault sat PodInitializing for 8+ days because the hive guard
# correctly suppressed pages ("local-only issue, system not down") but
# "local-only" doesn't mean "ignore forever" — the broken local instance still
# needs to recover. The restart reflex churns the pod every hour but never
# fixes deeper issues (stuck PVC binding, image pull, init container failures),
# so after enough hours of fruitless churn, a fresh ``delete pod`` is usually
# the only safe autonomous move (kubelet recreates from spec, PVC data is
# preserved). Default 24h is deliberately far beyond any normal recovery
# window: by this point the restart reflex has tried ~72 times and got nowhere,
# so the heavier delete-and-recreate is justified.
STUCK_RECOVERY_THRESHOLD_S = int(os.environ.get("STUCK_RECOVERY_THRESHOLD_S", "86400"))

# Minimum cooldown between successive auto-recovery deletes for the same
# incident. Without this we'd fire the delete every health tick once the
# threshold trips. 6h gives the rescheduled pod a full window to either
# recover or get stuck the exact same way again before we try once more.
STUCK_RECOVERY_COOLDOWN_S = int(os.environ.get("STUCK_RECOVERY_COOLDOWN_S", "21600"))

# Per-component opt-in for Tier-3 auto-recovery. Comma-separated list of
# component names. Originally defaulted to just "maki-vault" (#264 driver),
# but #311 exposed the cost of keeping it that narrow: maki-recall sat in
# CrashLoopBackOff locally for 13 days while the hive guard suppressed
# pages ("other site has recall healthy, local-only issue") and the narrow
# allowlist excluded recall from the autonomous pod-delete safety net —
# a dead zone where neither human nor autonomous recovery could reach it.
# We widen the default to the stateless application services where
# ``delete pod`` is safe (kubelet recreates from spec, no PVC concerns):
# the cortex/recall/stem/ears/synapse loop. We keep ``maki-vault`` for the
# original #264 case. We deliberately omit ``maki-immune`` itself —
# immune self-deleting mid-loop is a sharp corner that the kubelet liveness
# probe already covers, and we don't want the watcher killing itself
# inside its own delete syscall.
STUCK_RECOVERY_ALLOWLIST_DEFAULT = os.environ.get(
    "STUCK_RECOVERY_ALLOWLIST",
    "maki-vault,maki-recall,maki-cortex,maki-stem,maki-ears,maki-synapse",
)

# Multiplier on ``_check_interval`` after which a component's stale
# ``last_check`` is treated as an immune-self-failure (#270). The motivating
# case was the inverse of the usual outage: every reflection cycle ran
# correctly, every Claude tick observed recall=False with consecutive_failures
# climbing, but a one-shot probe from cortex got 200 OK from the same URL.
# That divergence — monitor's view of reality vs. reality — has to be
# self-correcting; without it the immune loop's state can drift for days with
# nobody noticing. 2× gives one full poll's slack for transient delays
# (scheduler hiccup, NATS reconnect) before treating it as a real wedge.
SELF_HEALTH_STALE_MULTIPLIER = float(os.environ.get("SELF_HEALTH_STALE_MULTIPLIER", "2"))

# Cooldown between successive self-health alerts for the same component. The
# stale check runs every health tick (~30s); without a cooldown a single
# wedged sub-task would fire an alert on every tick. 30 min default is long
# enough to avoid spam, short enough that a real silent stop becomes visible
# inside one nap. Matches the order-of-magnitude of the existing
# stuck/long-unhealthy realert intervals.
SELF_HEALTH_REALERT_INTERVAL_S = int(os.environ.get("SELF_HEALTH_REALERT_INTERVAL_S", "1800"))

# Terminal-zombie escalation tunables (#470). A "terminal zombie" is a pod that
# stays ``phase=Running, ready=False`` while its /health body advertises a
# permanent error the retry loop cannot fix (bad NATS token, missing TLS cert,
# invalid credentials file). The other autonomy tiers all skip this shape:
# ``_check_stuck_components`` only matches non-Running pods, ``_trigger_reflex``
# would just delete-and-recreate the pod against the same broken Secret, and
# ``_check_stuck_recovery`` bails on the same non-Running predicate. Left to
# rot, ``consecutive_failures`` ticks up forever and nothing acts (motivating
# incident: recall wedged 34+ min with 63 failures and zero restarts). The
# fix is a distinct check that spots the terminal marker in the /health body
# excerpt and escalates directly to Claude with a "config mismatch" hint —
# not a reflex restart, not a pod delete.
TERMINAL_ZOMBIE_MIN_FAILURES = int(os.environ.get("TERMINAL_ZOMBIE_MIN_FAILURES", "10"))

# Cooldown between re-fires for the same incident. First escalation is
# immediate once the failure threshold is crossed; after that we re-alert
# every ``TERMINAL_ZOMBIE_REALERT_INTERVAL_S`` so an ignored escalation
# doesn't go silent, without spamming every 30s health tick.
TERMINAL_ZOMBIE_REALERT_INTERVAL_S = int(os.environ.get("TERMINAL_ZOMBIE_REALERT_INTERVAL_S", "3600"))

# Substrings we consider "terminal error" markers in a component's /health
# body excerpt. Match is case-sensitive and substring-based so a component
# can advertise any of these shapes:
#   - ``"nats_terminal": true`` — recall's structured surface (see #470)
#   - ``"terminal": true``       — generic
#   - ``Authorization Violation`` / ``Authorization Timeout`` — raw NATS -ERR text
#   - ``TLS Required``            — server refused non-TLS handshake
# New shapes should be added here and to
# ``maki_common.nats._TERMINAL_NATS_MESSAGES`` together.
TERMINAL_ERROR_PATTERNS: tuple[str, ...] = (
    '"nats_terminal": true',
    '"terminal": true',
    "Authorization Violation",
    "Authorization Timeout",
    "TLS Required",
    "InvalidUserCredentialsError",
    "NatsTerminalError",
)

log = logging.getLogger(__name__)


class ImmuneHealthMonitor:
    """Owns component health tracking, reflexes, gossip and cortex watchdogs.

    Every per-incident tracker that used to sit as a module-level dict lives
    on the instance now, which means a fresh monitor really is fresh — no
    residual ``_stuck_alerted`` entries from a previous test run, no
    ``_token_stats`` cross-talk between simulated sites. The
    ``escalate_to_claude`` callback (formerly the ``_escalate_to_claude``
    module global used to sidestep a would-be import cycle) is just another
    dependency injected here; ``main.py`` hands us
    ``ImmuneClaudeReasoner.escalate_to_claude`` as a bound method.
    """

    def __init__(
        self,
        *,
        nc: Any,
        k8s_v1: Any,
        k8s_apps_v1: Any,
        namespace: str,
        instance_id: str,
        site_name: str,
        check_interval: int,
        gossip_stale_threshold: int,
        health_endpoints: dict[str, str],
        default_config: dict[str, Any],
        config_kv: Any,
        cortex_config_kv: Any,
        component_health: dict,
        pod_metrics: dict,
        restart_history: dict,
        recent_actions: list,
        recent_actions_max: int,
        running_images: dict,
        hive_state: dict,
        failed_image_blacklist: set,
        cortex_state: dict,
        infra_lock: Any,
        publish_alert: Any,
        publish_vitals: Any,
        schedule_persist_recent_actions: Any,
        escalate_to_claude: Any,
    ) -> None:
        self._nc = nc
        self._k8s_v1 = k8s_v1
        self._k8s_apps_v1 = k8s_apps_v1
        self._namespace = namespace
        self._instance_id = instance_id
        self._site_name = site_name
        self._check_interval = check_interval
        self._gossip_stale_threshold = gossip_stale_threshold
        self._health_endpoints = health_endpoints
        self._default_config = default_config
        self._config_kv = config_kv
        self._cortex_config_kv = cortex_config_kv  # stem's cortex config (chat_model etc.)
        self._component_health = component_health
        self._pod_metrics = pod_metrics
        self._restart_history = restart_history
        self._recent_actions = recent_actions
        self._recent_actions_max = recent_actions_max
        self._running_images = running_images
        self._hive_state = hive_state
        self._failed_image_blacklist = failed_image_blacklist
        self._cortex_state = cortex_state
        self._infra_lock = infra_lock
        self._publish_alert = publish_alert
        self._publish_vitals = publish_vitals
        self._schedule_persist = schedule_persist_recent_actions
        # ``escalate_to_claude`` is injected as a bound method from
        # :class:`ImmuneClaudeReasoner` — the ex-workaround-callback is now
        # just a normal collaborator with no circular-import baggage.
        self._escalate_to_claude = escalate_to_claude

        # --- Per-instance mutable state ---
        self._last_incident_time: float = 0

        # Token usage tracking — accumulated per day, reset on date change
        self._token_stats: dict[str, Any] = {
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
        self._cortex_stuck_alerted: dict[str, Any] = {
            "turn_id": None,
            "turn_started": None,
            "last_fired_at": 0.0,
            "fire_count": 0,
        }

        # Track which components we've raised a "stuck" escalation for. Keyed by component
        # name -> {incident, last_fired_at, fire_count}. ``incident`` is the component's
        # ``last_state_change`` at the time we first escalated, so a fresh outage (new
        # state change) re-arms cleanly rather than being suppressed by a stale entry.
        # Entries are dropped the moment the component recovers or stops being stuck. See #245.
        self._stuck_alerted: dict[str, dict[str, Any]] = {}

        # Track long-unhealthy re-escalations (#260). Same shape as ``_stuck_alerted``
        # but covers the broader case: any component unhealthy past
        # LONG_UNHEALTHY_RE_ESCALATE_HOURS, regardless of pod phase. Keyed by component
        # -> {incident, last_fired_at, fire_count}. ``incident`` is the component's
        # ``last_state_change`` at the time we first re-escalated so a recover-and-
        # fail-again cycle re-arms cleanly. In-memory only — a restart of immune costs
        # at most one extra alert per stuck component, which is acceptable.
        self._long_unhealthy_alerted: dict[str, dict[str, Any]] = {}

        # Track Tier-3 auto-recovery attempts (#264). Keyed by component name ->
        # {incident, last_attempted_at, count}. ``incident`` is the component's
        # ``last_state_change`` so a fresh outage re-arms cleanly. Dropped when the
        # component recovers. In-memory only — if immune restarts mid-incident we
        # might fire one extra recovery, which is the safe direction (the whole point
        # is to unwedge a pod that's been dead for a day).
        self._stuck_recovery_attempts: dict[str, dict[str, Any]] = {}

        # Track immune-self-failure alerts (#270). Keyed by component name ->
        # {first_seen_stale_at, last_fired_at, fire_count}. Cleared when the
        # component's ``last_check`` advances again. In-memory only; if immune
        # restarts during a wedge we'll re-fire one alert per stuck component, which
        # is fine — that's the whole point.
        self._self_health_alerted: dict[str, dict[str, Any]] = {}

        # Track terminal-zombie escalations (#470). Same shape as
        # ``_stuck_alerted``: keyed by component -> {incident, last_fired_at,
        # fire_count}. Cleared when the component recovers so a subsequent
        # failure re-arms cleanly. In-memory only.
        self._terminal_zombie_alerted: dict[str, dict[str, Any]] = {}

    # --- Health State Tracking ---

    def _update_health(self, component: str, healthy: bool, details: dict | None = None) -> None:
        """Update component health state and detect transitions."""
        now = time.time()

        if component not in self._component_health:
            self._component_health[component] = {
                "healthy": healthy,
                "last_check": now,
                "last_state_change": now,
                "consecutive_failures": 0 if healthy else 1,
                "details": details or {},
            }
            return

        state = self._component_health[component]
        was_healthy = state["healthy"]

        if was_healthy and not healthy:
            state["last_state_change"] = now
            state["consecutive_failures"] = 1
            self._last_incident_time = now
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

    def get_last_incident_time(self) -> float:
        return self._last_incident_time

    # --- Health Checks ---

    async def _check_http_health(self) -> dict[str, tuple[bool, dict[str, Any]]]:
        """Check HTTP health endpoints and return per-component verdicts.

        Returns ``{component: (healthy, details)}`` rather than writing to
        ``_component_health`` directly. The monitor loop combines this with the
        K8s pod verdict via :meth:`_merge_and_update_health` before calling
        :meth:`_update_health` once per component — see #249. Previously both
        checkers wrote to the same key and the one that fired last clobbered
        the other's verdict, which kept ``consecutive_failures`` flapping at
        1–2 and silently suppressed the reflex engine when HTTP and K8s
        disagreed.

        On a non-200 response we capture a truncated body excerpt and log it so
        the actual failure mode is visible — this is the asking-the-server-
        what-it-said fix from #270, where the immune monitor reported recall as
        503-for-5-days while a one-shot probe from cortex returned 200. Without
        the body we had no way to distinguish "Mem0 graph init failed" from
        "Kubernetes routed the probe at the wrong pod" — we just saw a number
        tick up.

        On a transport exception (timeout, connection refused, DNS) we record
        the exception class on details so the same investigation works for the
        unreachable case.
        """
        verdicts: dict[str, tuple[bool, dict[str, Any]]] = {}
        async with httpx.AsyncClient(timeout=5.0) as client:
            for component, url in self._health_endpoints.items():
                try:
                    start = time.time()
                    resp = await client.get(f"{url}/health")
                    latency_ms = round((time.time() - start) * 1000, 1)
                    healthy = resp.status_code == 200
                    details: dict[str, Any] = {
                        "latency_ms": latency_ms,
                        "status_code": resp.status_code,
                    }
                    if not healthy:
                        # Truncate to keep logs/state small; the failure mode is
                        # usually obvious in the first ~200 chars (status string,
                        # exception type). Body may be html, json, or plain text —
                        # don't try to parse, just preserve verbatim.
                        body = resp.text[:200]
                        details["body_excerpt"] = body
                        log.warning(
                            "HTTP health probe failed",
                            extra={
                                "component": component,
                                "url": f"{url}/health",
                                "status_code": resp.status_code,
                                "latency_ms": latency_ms,
                                "body_excerpt": body,
                            },
                        )
                    verdicts[component] = (healthy, details)
                except Exception as e:
                    err_type = type(e).__name__
                    err_msg = str(e)[:200]
                    log.warning(
                        "HTTP health probe errored",
                        extra={
                            "component": component,
                            "url": f"{url}/health",
                            "error_type": err_type,
                            "error": err_msg,
                        },
                    )
                    verdicts[component] = (
                        False,
                        {"latency_ms": -1, "error_type": err_type, "error": err_msg},
                    )
        return verdicts

    async def _check_k8s_pods(self) -> dict[str, tuple[bool, dict[str, Any]]]:
        """Check K8s pod status in maki namespace and return per-app verdicts.

        Returns ``{app_label: (healthy, details)}`` instead of mutating
        ``_component_health`` directly. The monitor loop merges this with the
        HTTP verdict via :meth:`_merge_and_update_health` so a single composite
        call decides each component's health per tick — see #249 for the race
        the old direct-write pattern caused (consecutive_failures pinned to
        1–2 when HTTP and K8s disagreed, reflex engine silently suppressed).

        :meth:`_check_pod_metrics` is still called serially at the end so the
        metrics map stays fresh for whoever observes it.
        """
        verdicts: dict[str, tuple[bool, dict[str, Any]]] = {}
        if not self._k8s_v1:
            return verdicts
        try:
            pods = await asyncio.to_thread(self._k8s_v1.list_namespaced_pod, namespace=self._namespace)

            app_pods: dict[str, list[dict]] = {}
            for pod in pods.items:
                app_label = pod.metadata.labels.get("app", "") if pod.metadata.labels else ""
                if not app_label:
                    continue

                # Skip pods that aren't part of the live serving set so the verdict
                # tracks the same reality the Service routes to (#297). Without this,
                # stuck Terminating pods (finalizer wedge, kubelet GC lag) and old
                # ReplicaSet leftovers in Succeeded/Failed keep appearing as
                # "unhealthy" replicas of the app long after the current live pod
                # has taken over. The motivating case: maki-recall reported
                # ``phase=Running, ready=False, waiting_reason=CrashLoopBackOff,
                # total_pods=2, unhealthy_pods=2`` for 22 days while ``/health``
                # returned 200 from the actual live pod — immune was merging the
                # live pod with a stale one whose containers had been wedged in
                # CrashLoopBackOff since the previous rollout, picking the stale
                # one as the "report pod" (first unhealthy), and the composite
                # ``http_ok AND k8s_ok`` verdict came out False on every tick.
                #
                # ``deletion_timestamp`` covers Terminating pods (even ones stuck
                # for days); phase ``Succeeded``/``Failed`` covers terminally
                # completed pods that haven't been GC'd yet. Pods still in flight
                # (Pending/PodInitializing/Running) stay — the existing
                # ``_check_stuck_components`` path already handles the case of a
                # legitimately-stuck Pending pod.
                if pod.metadata.deletion_timestamp is not None:
                    continue
                if pod.status.phase in ("Succeeded", "Failed"):
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

                verdicts[app_label] = (all_healthy, details)
        except Exception:
            log.exception("K8s pod check failed")

        await self._check_pod_metrics()
        return verdicts

    def _merge_and_update_health(
        self,
        http_verdicts: dict[str, tuple[bool, dict[str, Any]]],
        k8s_verdicts: dict[str, tuple[bool, dict[str, Any]]],
    ) -> None:
        """Fold HTTP and K8s verdicts into one ``_update_health`` call per component.

        ``healthy`` is ``http_ok AND k8s_ok`` when both probes apply, otherwise
        whichever single verdict exists. Details from both checkers are merged
        so observers see the full picture even when the verdicts disagree (e.g.
        ``status_code=200`` alongside ``waiting_reason=CrashLoopBackOff,
        restarts=12``). HTTP keys take precedence on the rare overlap; in
        practice the two detail sets are disjoint.

        Background: previously ``_check_http_health`` and ``_check_k8s_pods``
        each called ``_update_health(component, ...)`` directly. When they
        disagreed — HTTP 200 vs ``CrashLoopBackOff`` between restarts,
        transient mem0 503 on an otherwise-healthy pod, kubelet readiness lag —
        whichever checker fired last clobbered the other,
        ``consecutive_failures`` reset to 1 every tick, and the reflex
        pipeline's ``>= 2`` gate never engaged. The composite verdict here is
        the cleanest of the three fixes sketched in #249 because a disagreement
        now produces an honest "unhealthy" with both signals recorded —
        exactly what the reflex engine needs to act on, and what Claude needs
        to read in :class:`ImmuneClaudeReasoner`.
        """
        components = set(http_verdicts) | set(k8s_verdicts)
        for component in components:
            http = http_verdicts.get(component)
            k8s = k8s_verdicts.get(component)
            if http is not None and k8s is not None:
                healthy = http[0] and k8s[0]
                # k8s details first so HTTP keys (status_code, latency_ms,
                # body_excerpt) override on the unlikely overlap and the pod_name
                # the reflex engine needs is preserved.
                details: dict[str, Any] = {**k8s[1], **http[1]}
                if http[0] != k8s[0]:
                    # Surface the disagreement to whoever reads details (Claude
                    # via the reasoner, the snapshot publisher). Without this
                    # the composite verdict hides which probe disagreed.
                    details["http_ok"] = http[0]
                    details["k8s_ok"] = k8s[0]
            elif http is not None:
                healthy, details = http
            else:
                # Mypy/ty: at least one of http/k8s is non-None because component
                # came from the union of their keys.
                assert k8s is not None
                healthy, details = k8s
            self._update_health(component, healthy, details)

    async def _check_pod_metrics(self) -> None:
        """Fetch pod resource usage from K8s metrics API."""
        try:
            custom_api = k8s_client.CustomObjectsApi()
            metrics = await asyncio.to_thread(
                custom_api.list_namespaced_custom_object,
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=self._namespace,
                plural="pods",
            )
            self._pod_metrics.clear()
            for item in metrics.get("items", []):
                pod_name = item["metadata"]["name"]
                containers = item.get("containers", [])
                if containers:
                    self._pod_metrics[pod_name] = {
                        "cpu": containers[0].get("usage", {}).get("cpu", "0"),
                        "memory": containers[0].get("usage", {}).get("memory", "0"),
                    }
        except Exception:
            pass

    # --- Cortex Heartbeat ---

    def _fresh_hive_peers(self) -> Iterator[tuple[str, dict]]:
        """Yield (site, state) for peers whose gossip is within the stale threshold.

        Read-time freshness filter — the invariant "only fresh peers count"
        must hold everywhere ``_hive_state`` is consulted, because the only
        pruner runs inside ``_handle_gossip`` and fires *on receipt*. If gossip
        stops arriving (NATS partition, all peer immunes down, the
        ``subscribe_supervised`` loop drops without re-subscribing, or
        ``_handle_gossip`` raises before reaching the prune), entries retain
        their old ``received_at`` forever. Downstream reads — most dangerously
        the ``_check_stuck_recovery`` sanity gate that authorises an
        autonomous ``delete pod`` — would then treat those stale entries as
        live peers confirming the recipe works, when in reality nobody has
        reported in for hours. Filtering at read time is O(peers), cheap, and
        eliminates the "prune fires only on receipt" trap with no timing gap.
        """
        now = time.time()
        for site, state in self._hive_state.items():
            received_at = state.get("received_at", 0)
            if now - received_at <= self._gossip_stale_threshold:
                yield site, state

    def _hive_cortex_status(self) -> tuple[int, int]:
        """Count how many hive sites (local + remote peers) have healthy cortex."""
        # Include local site
        total = 1
        local_last = self._cortex_state["last_heartbeat"]
        healthy = 1 if local_last and (time.time() - local_last) < 60 else 0
        # Include remote peers from hive gossip (freshness-filtered — stale
        # entries would otherwise inflate the cortex heartbeat health signal).
        for _site, state in self._fresh_hive_peers():
            total += 1
            cortex_info = state.get("cortex", {})
            age = cortex_info.get("last_heartbeat_age_s")
            if age is not None and age < 60:
                healthy += 1
        return total, healthy

    def component_healthy_in_hive(self, component: str) -> list[str]:
        """Return list of hive site names where this component is healthy.

        Filtered to fresh peers only — a stale entry here would authorise the
        stuck-recovery ``delete pod`` path (``_check_stuck_recovery``) on the
        basis of a peer that hasn't gossiped in hours.
        """
        healthy_sites = []
        for site, state in self._fresh_hive_peers():
            peer_health = state.get("component_health", {})
            comp_state = peer_health.get(component, {})
            if comp_state.get("healthy"):
                healthy_sites.append(site)
        return healthy_sites

    def _check_cortex_heartbeat(self) -> None:
        """Check if cortex heartbeat is recent and include turn state + hive context.

        Also acts as the layer-2 stuck-turn watchdog: if a turn has been
        running longer than ``CORTEX_STUCK_THRESHOLD_S`` we publish
        ``CORTEX_STUCK`` so the existing escalation path
        (``cortex_stuck_handler`` on the reasoner) can decide what to do —
        restart the pod, page Adi, etc. This is the safety net for the case
        where cortex's own internal timeout failed to fire (e.g., uncancellable
        native call). Only the active turn is tracked, and we alert once per
        (turn_id, turn_started) tuple to avoid spamming every tick.
        """
        if self._cortex_state["last_heartbeat"] == 0:
            return
        now = time.time()
        age = now - self._cortex_state["last_heartbeat"]
        hive_total, hive_healthy = self._hive_cortex_status()
        details: dict = {
            "last_heartbeat_age_s": round(age, 1),
            "hive_cortex_total": hive_total,
            "hive_cortex_healthy": hive_healthy,
        }
        active_turn = self._cortex_state["active_turn"]
        turn_mode = self._cortex_state["turn_mode"]
        turn_started = self._cortex_state["turn_started"]
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

        self._update_health("maki-cortex-heartbeat", local_healthy, details)

        # Reset stuck-alerted tracking when the active turn changes (or clears).
        if (
            self._cortex_stuck_alerted["turn_id"] != active_turn
            or self._cortex_stuck_alerted["turn_started"] != turn_started
        ):
            self._cortex_stuck_alerted["turn_id"] = active_turn
            self._cortex_stuck_alerted["turn_started"] = turn_started
            self._cortex_stuck_alerted["last_fired_at"] = 0.0
            self._cortex_stuck_alerted["fire_count"] = 0

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
            and self._nc is not None
        ):
            last_fired = self._cortex_stuck_alerted.get("last_fired_at") or 0.0
            time_since_last = now - last_fired
            first_fire = last_fired == 0.0
            # Fire on first crossing, then re-arm every threshold interval.
            if first_fire or time_since_last >= CORTEX_STUCK_THRESHOLD_S:
                fire_count = int(self._cortex_stuck_alerted.get("fire_count") or 0) + 1
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
                # ``spawn_background`` anchors the publish task against GC and
                # logs any uncaught exception (issue #123).
                try:
                    spawn_background(self._nc.publish(CORTEX_STUCK, payload), name="immune.cortex_stuck_publish")
                except Exception:
                    log.exception("Failed to schedule CORTEX_STUCK publish")
                self._cortex_stuck_alerted["last_fired_at"] = now
                self._cortex_stuck_alerted["fire_count"] = fire_count

    async def _handle_cortex_heartbeat(self, msg) -> None:
        self._cortex_state["last_heartbeat"] = time.time()
        payload = json.loads(msg.data.decode())
        self._cortex_state["active_turn"] = payload.get("active_turn")
        self._cortex_state["turn_mode"] = payload.get("turn_mode")
        self._cortex_state["turn_started"] = payload.get("turn_started")

    async def cortex_heartbeat_listener(self) -> None:
        """Subscribe to cortex health heartbeat and parse enriched turn state.

        Wrapped in ``subscribe_supervised`` so a silent subscription drop or a
        NATS client close re-subscribes with backoff instead of leaving the task
        finished and ``_cortex_state["last_heartbeat"]`` frozen — which would
        flip ``maki-cortex-heartbeat`` unhealthy and could trigger a reflex
        restart on a perfectly healthy cortex (issue #175).
        """
        from maki_common.subjects import CORTEX_HEALTH

        await subscribe_supervised(
            self._nc,
            CORTEX_HEALTH,
            self._handle_cortex_heartbeat,
            # Cheap dict mutation. Five seconds catches a wedge without
            # tolerating the "frozen last_heartbeat + reflex restart"
            # failure mode the supervised loop was added to prevent
            # (#492 / #175).
            handler_timeout=5.0,
            name="cortex_heartbeat",
        )

    async def _handle_token_usage(self, msg) -> None:
        payload = json.loads(msg.data.decode())
        today = datetime.now().strftime("%Y-%m-%d")
        if self._token_stats["date"] != today:
            self._token_stats["date"] = today
            self._token_stats["total_tokens"] = 0
            self._token_stats["total_cost_usd"] = 0.0
            self._token_stats["turns"] = 0
            self._token_stats["by_model"] = {}

        tokens = payload.get("total_tokens", 0)
        cost = payload.get("total_cost_usd", 0.0)
        model = payload.get("model", "unknown")

        self._token_stats["total_tokens"] += tokens
        self._token_stats["total_cost_usd"] += cost
        self._token_stats["turns"] += 1

        if model not in self._token_stats["by_model"]:
            self._token_stats["by_model"][model] = {"tokens": 0, "cost_usd": 0.0, "turns": 0}
        self._token_stats["by_model"][model]["tokens"] += tokens
        self._token_stats["by_model"][model]["cost_usd"] += cost
        self._token_stats["by_model"][model]["turns"] += 1

    async def token_usage_listener(self) -> None:
        """Subscribe to cortex token usage and accumulate daily stats."""
        subject = f"{CORTEX_TOKEN_USAGE}.{self._site_name}"
        await subscribe_supervised(
            self._nc,
            subject,
            self._handle_token_usage,
            # Pure in-memory accumulator. Five seconds is generous (#492).
            handler_timeout=5.0,
            name="token_usage",
        )

    # --- Health Monitor Loop ---

    async def health_monitor_loop(self) -> None:
        """Continuous health monitoring — no Claude, triggers reflexes."""
        log.info(
            "Health monitor loop started", extra={"interval": self._check_interval, "instance_id": self._instance_id}
        )

        while True:
            try:
                # Gather both verdicts before writing — see #249 and
                # _merge_and_update_health for why the direct-write pattern was
                # racy. Sequential for now; #231 tracks parallelising the HTTP
                # fan-out separately.
                http_verdicts = await self._check_http_health()
                k8s_verdicts = await self._check_k8s_pods()
                self._merge_and_update_health(http_verdicts, k8s_verdicts)
                self._check_cortex_heartbeat()

                config = await load_kv_config(self._config_kv, self._default_config)
                for component, state in self._component_health.items():
                    if not state["healthy"] and state["consecutive_failures"] >= 2:
                        await self._trigger_reflex(component, state, config)

                await self._check_stuck_components(config)
                await self._check_terminal_zombies(config)
                await self._check_long_unhealthy_components(config)
                await self._check_immune_self_health(config)
                await self._check_stuck_recovery(config)

            except Exception:
                log.exception("Health monitor error")

            await asyncio.sleep(self._check_interval)

    # --- Reflex Engine ---

    async def _trigger_reflex(self, component: str, state: dict, config: dict) -> None:
        """Autonomous pod restart reflex (Tier 1)."""
        if component.endswith("-heartbeat"):
            return

        # #470: don't reflex-restart a terminal-zombie. If /health already
        # tells us the failure is a permanent config mismatch (bad NATS
        # token, missing TLS cert), delete-and-recreate just schedules a
        # fresh pod against the same broken Secret and hits the same
        # -ERR — pointless thrash that also masks the incident with a
        # stream of restarts and rotating pod names. The
        # ``_check_terminal_zombies`` path owns escalation for this
        # shape; log at DEBUG here so the skip is greppable but doesn't
        # add tick-rate noise.
        details = state.get("details", {})
        if details.get("phase") == "Running" and details.get("ready") is False:
            terminal_pattern = self._detect_terminal_pattern(details)
            if terminal_pattern is not None:
                log.debug(
                    "Skipping reflex restart — terminal error in /health body",
                    extra={
                        "component": component,
                        "terminal_pattern": terminal_pattern,
                    },
                )
                return

        pod_name = state.get("details", {}).get("pod_name")
        if not pod_name:
            return

        now = time.time()
        hour_ago = now - 3600
        max_restarts = config.get("reflex_restart_max", 3)

        history = self._restart_history.get(component, [])
        history = [t for t in history if t > hour_ago]
        self._restart_history[component] = history

        if len(history) >= max_restarts:
            hive_healthy_elsewhere = self.component_healthy_in_hive(component)
            if hive_healthy_elsewhere:
                log.warning(
                    "Reflex limit reached but component healthy on other sites — skipping escalation",
                    extra={
                        "component": component,
                        "restarts": len(history),
                        "hive_healthy_sites": hive_healthy_elsewhere,
                    },
                )
                return

            log.warning(
                "Reflex limit reached, escalating to Claude",
                extra={"component": component, "restarts": len(history), "max": max_restarts},
            )
            await self._publish_alert(
                f"Reflex limit reached for {component}: {len(history)} restarts in last hour, escalating to Claude"
            )
            spawn_background(
                self._escalate_to_claude(
                    component,
                    state,
                    f"Reflex restart limit reached ({len(history)}/{max_restarts} restarts in last hour)",
                ),
                name="immune.reflex_limit_escalation",
            )
            return

        try:
            async with self._infra_lock("immune-reflex", ttl=60):
                try:
                    history.append(now)
                    self._restart_history[component] = history

                    await asyncio.to_thread(
                        self._k8s_v1.delete_namespaced_pod,
                        name=pod_name,
                        namespace=self._namespace,
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
                    self._recent_actions.append(action)
                    if len(self._recent_actions) > self._recent_actions_max:
                        self._recent_actions.pop(0)
                    self._schedule_persist()

                    log.info(
                        "Reflex restart",
                        extra={
                            "component": component,
                            "pod_name": pod_name,
                            "restart_number": len(history),
                            "max": max_restarts,
                        },
                    )
                    await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())

                except Exception:
                    log.exception("Failed to restart pod", extra={"pod_name": pod_name})
        except LockNotAcquired:
            log.warning("Cannot acquire lock for reflex restart", extra={"component": component})
            return

    # --- Stuck-Component Detection (Tier 1.5) ---

    @staticmethod
    def _healthy_replica_count(details: dict) -> tuple[int, int]:
        """Return (healthy_replicas, total_replicas) for an unhealthy component.

        The k8s pod check records ``total_pods``/``unhealthy_pods`` only when a
        component has more than one pod. For a single-pod component those keys are
        absent — and since this is only called for an *unhealthy* component, that
        single pod is by definition not healthy, so we report (0, 1).
        """
        total = details.get("total_pods")
        if total is None:
            return 0, 1
        unhealthy = details.get("unhealthy_pods") or 0
        return max(total - unhealthy, 0), total

    async def _check_stuck_components(self, config: dict) -> None:
        """Escalate components stuck unhealthy in a non-Running phase with no healthy replica.

        The restart reflex (``_trigger_reflex``) handles pods that crash and restart,
        but a pod that *never finishes initializing* (phase=Pending,
        waiting_reason=PodInitializing, restarts=0) produces no crash signal and can
        sit dead for days while only ``consecutive_failures`` ticks up — exactly the
        autonomy gap in #245. Here we classify that as "stuck" off ``last_state_change``
        (wall-clock duration, not failure count) and escalate:

        - **Human escalation** — publish an ALERT, which reaches Adi via ears. This is
          the path that was missing: a foundational component could be dead for days
          with nobody notified.
        - **Judgment-driven remediation** — hand the incident to immune's own Claude,
          which can read logs/events and decide whether deleting the stuck pod is safe
          (blindly deleting a single-replica secrets StatefulSet is risky), rather than
          a blind reflex restart.

        Guards that keep this from firing on healthy rollouts (#77) or localized
        issues: we only escalate when the pod is in a non-Running/initializing phase,
        has been stuck longer than the threshold, has **zero** healthy replicas, and
        is not healthy on any other hive site.
        """
        if self._nc is None:
            return

        threshold = config.get("stuck_escalation_threshold_s", STUCK_ESCALATION_THRESHOLD_S)
        realert = config.get("stuck_realert_interval_s", STUCK_REALERT_INTERVAL_S)
        now = time.time()

        for component, state in list(self._component_health.items()):
            if component.endswith("-heartbeat"):
                continue

            if state["healthy"]:
                self._stuck_alerted.pop(component, None)
                continue

            details = state.get("details", {})
            phase = details.get("phase")
            waiting_reason = details.get("waiting_reason")

            # Only the never-finishes-initializing / non-Running pod case. A pod that
            # is Running+Ready but failing its HTTP health check is a different failure
            # mode that the restart reflex already handles; escalating it here too would
            # double up. Components with no phase at all (pure HTTP endpoints) are
            # likewise left to the reflex path.
            is_pod_stuck = (phase is not None and phase != "Running") or waiting_reason is not None
            if not is_pod_stuck:
                self._stuck_alerted.pop(component, None)
                continue

            stuck_for = now - state["last_state_change"]
            if stuck_for < threshold:
                continue

            # #77 guard: only escalate when NO replica is serving. A healthy rolling
            # update keeps other pods ready, so healthy_replicas > 0 and we bail.
            healthy_replicas, total_replicas = self._healthy_replica_count(details)
            if healthy_replicas > 0:
                self._stuck_alerted.pop(component, None)
                continue

            # Hive guard: if the component is healthy on another site it's a local
            # problem, not a system-wide outage — matches the reflex escalation policy.
            if self.component_healthy_in_hive(component):
                continue

            # Re-arm tracking: fire once on first crossing for this incident, then every
            # ``realert`` interval until it recovers — so a single ignored escalation
            # doesn't go silent, without spamming on every 30s tick.
            incident = state["last_state_change"]
            tracker = self._stuck_alerted.get(component)
            if tracker is None or tracker.get("incident") != incident:
                tracker = {"incident": incident, "last_fired_at": 0.0, "fire_count": 0}
                self._stuck_alerted[component] = tracker

            last_fired = tracker.get("last_fired_at") or 0.0
            first_fire = last_fired == 0.0
            if not first_fire and (now - last_fired) < realert:
                continue

            fire_count = int(tracker.get("fire_count") or 0) + 1
            tracker["last_fired_at"] = now
            tracker["fire_count"] = fire_count

            stuck_min = round(stuck_for / 60, 1)
            pod_name = details.get("pod_name", "?")
            restarts = details.get("restarts", 0)
            replica_note = f"{healthy_replicas}/{total_replicas} replicas healthy"

            log.error(
                "Stuck component detected — escalating",
                extra={
                    "component": component,
                    "pod_name": pod_name,
                    "stuck_for_min": stuck_min,
                    "phase": phase,
                    "waiting_reason": waiting_reason,
                    "restarts": restarts,
                    "fire_count": fire_count,
                },
            )

            # Human escalation via ears (#245's missing piece).
            await self._publish_alert(
                f"STUCK: {component} ({pod_name}) has been {phase or 'not Running'}"
                f"/{waiting_reason or 'unhealthy'} for {stuck_min}min with {replica_note} — "
                f"it never finished initializing (restarts={restarts}), no crash signal. "
                f"Escalation #{fire_count}; needs attention."
            )

            # Audit the escalation as a recent action.
            action = {
                "type": "stuck_escalation",
                "component": component,
                "pod_name": pod_name,
                "stuck_for_min": stuck_min,
                "phase": phase,
                "waiting_reason": waiting_reason,
                "restarts": restarts,
                "fire_count": fire_count,
                "timestamp": now,
            }
            self._recent_actions.append(action)
            if len(self._recent_actions) > self._recent_actions_max:
                self._recent_actions.pop(0)
            self._schedule_persist()
            try:
                await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
            except Exception:
                log.exception("Failed to publish stuck escalation action")

            # Judgment-driven remediation: only on the first fire per incident, so we
            # don't spawn a Claude turn every re-alert. Claude can investigate and, if
            # safe, restart the pod via its tools.
            if first_fire and self._escalate_to_claude is not None:
                reason = (
                    f"Stuck classification: {component} ({pod_name}) has been unhealthy for "
                    f"{stuck_min}min in phase={phase}, waiting_reason={waiting_reason}, "
                    f"restarts={restarts}, with {replica_note}. No crash signal — it never "
                    f"finished initializing, so the restart reflex never fired. Investigate "
                    f"(logs, events, init containers, dependencies) and remediate if safe."
                )
                spawn_background(
                    self._escalate_to_claude(component, state, reason),
                    name="immune.stuck_escalation",
                )

    @staticmethod
    def _detect_terminal_pattern(details: dict) -> str | None:
        """Return the first terminal-error pattern found in ``details['body_excerpt']``.

        The body excerpt is captured by :meth:`_check_http_health` on any
        non-200 response and truncated to 200 chars. Terminal markers
        (recall's ``"nats_terminal": true`` field, raw NATS -ERR text like
        ``Authorization Violation``, or a bare ``NatsTerminalError``
        surfaced in a traceback) live inside that window, so a substring
        match is enough. Returns ``None`` when no marker is present or when
        the component has no body excerpt (e.g. a transport-level error
        that never reached a response body).
        """
        body = details.get("body_excerpt")
        if not body:
            return None
        for pattern in TERMINAL_ERROR_PATTERNS:
            if pattern in body:
                return pattern
        return None

    async def _check_terminal_zombies(self, config: dict) -> None:
        """Escalate ``Running+ready=False+terminal=true`` components (#470).

        The other autonomy tiers all skip a Running-but-not-ready pod that's
        advertising a permanent error:

        - ``_trigger_reflex`` restarts it — but a fresh pod hits the same
          broken NATS token / TLS cert / credentials file and wedges the
          same way. #470 gates the reflex on the terminal marker so we
          don't churn.
        - ``_check_stuck_components`` requires ``phase != Running`` or a
          ``waiting_reason``. A Running+ready=False pod matches neither.
        - ``_check_stuck_recovery`` uses the same ``is_pod_stuck``
          predicate and skips for the same reason (motivating incident:
          recall 34+ min unhealthy, ``consecutive_failures=63``,
          ``restarts=0`` — nothing acted).

        The remediation for a config-level failure is *not* kubectl —
        it's a Secret/ConfigMap change. So this path escalates directly
        to Claude with a config-mismatch hint and publishes the same
        alert shape ears already renders, without ever asking the
        kubelet to recreate the pod. First-fire spawns the Claude turn;
        subsequent fires (past ``TERMINAL_ZOMBIE_REALERT_INTERVAL_S``)
        only re-alert so a single stalled Claude turn doesn't leave the
        incident silent for hours.
        """
        if self._nc is None:
            return

        threshold = config.get("terminal_zombie_min_failures", TERMINAL_ZOMBIE_MIN_FAILURES)
        realert = config.get("terminal_zombie_realert_interval_s", TERMINAL_ZOMBIE_REALERT_INTERVAL_S)
        now = time.time()

        for component, state in list(self._component_health.items()):
            if component.endswith("-heartbeat"):
                continue

            if state["healthy"]:
                self._terminal_zombie_alerted.pop(component, None)
                continue

            details = state.get("details", {})
            phase = details.get("phase")
            ready = details.get("ready")

            # Only the Running+ready=False shape — the very gap the other
            # tiers miss. Anything with a non-Running phase or a
            # waiting_reason is already covered by
            # ``_check_stuck_components`` / ``_check_stuck_recovery``.
            if phase != "Running" or ready is not False:
                self._terminal_zombie_alerted.pop(component, None)
                continue

            pattern = self._detect_terminal_pattern(details)
            if pattern is None:
                self._terminal_zombie_alerted.pop(component, None)
                continue

            consecutive_failures = state.get("consecutive_failures", 0)
            if consecutive_failures < threshold:
                # Wait until we're past the fluke threshold — a single
                # tick with a suspicious body could still be a rollout
                # racing with a stale probe. ``threshold`` (default 10)
                # ≈ 5 minutes of continuous failures at the 30s tick.
                continue

            incident = state["last_state_change"]
            tracker = self._terminal_zombie_alerted.get(component)
            if tracker is None or tracker.get("incident") != incident:
                tracker = {"incident": incident, "last_fired_at": 0.0, "fire_count": 0}
                self._terminal_zombie_alerted[component] = tracker

            last_fired = tracker.get("last_fired_at") or 0.0
            first_fire = last_fired == 0.0
            if not first_fire and (now - last_fired) < realert:
                continue

            fire_count = int(tracker.get("fire_count") or 0) + 1
            tracker["last_fired_at"] = now
            tracker["fire_count"] = fire_count

            stuck_for = now - state["last_state_change"]
            stuck_min = round(stuck_for / 60, 1)
            pod_name = details.get("pod_name", "?")
            body_excerpt = (details.get("body_excerpt") or "")[:200]

            log.error(
                "Terminal-zombie detected — escalating (config mismatch, not restartable)",
                extra={
                    "component": component,
                    "pod_name": pod_name,
                    "phase": phase,
                    "ready": ready,
                    "consecutive_failures": consecutive_failures,
                    "stuck_for_min": stuck_min,
                    "terminal_pattern": pattern,
                    "fire_count": fire_count,
                    "body_excerpt": body_excerpt,
                },
            )

            await self._publish_alert(
                f"TERMINAL-ZOMBIE: {component} ({pod_name}) has been Running+ready=False for "
                f"{stuck_min}min with a terminal error in its /health body "
                f"(pattern={pattern!r}, consecutive_failures={consecutive_failures}). "
                f"Restarting the pod won't help — remediation is a config/secret change "
                f"(likely NATS token, TLS cert, or credentials file). "
                f"Escalation #{fire_count}. /health excerpt: {body_excerpt}"
            )

            action = {
                "type": "terminal_zombie_escalation",
                "component": component,
                "pod_name": pod_name,
                "stuck_for_min": stuck_min,
                "phase": phase,
                "ready": ready,
                "consecutive_failures": consecutive_failures,
                "terminal_pattern": pattern,
                "fire_count": fire_count,
                "timestamp": now,
            }
            self._recent_actions.append(action)
            if len(self._recent_actions) > self._recent_actions_max:
                self._recent_actions.pop(0)
            self._schedule_persist()
            try:
                await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
            except Exception:
                log.exception("Failed to publish terminal-zombie action")

            # Only spawn the Claude turn on the first fire per incident.
            # Re-alerts still keep the audit trail loud, but a busy Claude
            # semaphore doesn't need one extra queued turn every realert
            # window for the same underlying config problem.
            if first_fire and self._escalate_to_claude is not None:
                reason = (
                    f"Terminal-zombie classification: {component} ({pod_name}) is stuck "
                    f"Running+ready=False with a terminal error surfaced in /health "
                    f"(pattern={pattern!r}). Recent /health body excerpt: {body_excerpt}. "
                    f"consecutive_failures={consecutive_failures}, stuck_for_min={stuck_min}. "
                    f"This is a CONFIG-LEVEL failure, not a transient outage: `kubectl "
                    f"delete pod` will re-schedule against the same broken "
                    f"Secret/ConfigMap and hit the same error immediately. "
                    f"Do NOT reflex-restart or delete the pod — investigate the "
                    f"pod's environment (Secret contents, ConfigMap, NATS token, "
                    f"TLS/creds file mounts) and remediate the config before rolling. "
                    f"If the fix requires a human action, publish an ALERT and stop."
                )
                spawn_background(
                    self._escalate_to_claude(component, state, reason),
                    name="immune.terminal_zombie_escalation",
                )

    async def _check_long_unhealthy_components(self, config: dict) -> None:
        """Re-escalate components that have been unhealthy for a long time (#260).

        Plugs the autonomy gap exposed by #259: the restart reflex (`_trigger_reflex`)
        only fires ``_escalate_to_claude`` once per restart-budget exhaustion. After
        that, ``consecutive_failures`` ticks up forever with no further alert. If the
        resulting incident issue gets closed without a root-cause fix (or escalation
        never reaches a human), the component can sit broken for days — exactly how
        maki-recall went 3.5d silent in this incident series (#251 → #257 → #259).

        Distinct from ``_check_stuck_components``: that one only fires for pods in a
        non-Running / initializing phase. This catches the broader case — *anything*
        immune classifies as unhealthy, whether it's an HTTP probe returning 503, a
        CrashLoopBackOff, or anything else — once it has been unhealthy past
        ``LONG_UNHEALTHY_RE_ESCALATE_HOURS`` since the last state change.

        Cooldown via ``_long_unhealthy_alerted`` keyed by component ensures one alert
        per ``long_unhealthy_realert_interval_s`` window, not one per 30s tick. The
        tracker is keyed on the component's ``last_state_change`` so a recover-and-
        fail-again cycle re-arms cleanly.

        Hive guard is tiered (#311). For the first
        ``long_unhealthy_local_only_escalate_hours`` (default 72h) we suppress when
        a peer site has the component healthy — matches the reflex/stuck-escalation
        "local-only, don't page" policy. Past that, we escalate anyway with a
        distinct ``LOCAL-ONLY-LONG-UNHEALTHY`` label, because the original
        suppression implicitly assumed *something* else would heal the local
        instance; for components without an autonomous recovery path nothing ever
        does, and the silence itself becomes the bug (recall sat in CrashLoopBackOff
        locally for 13 days before this gap was closed).
        """
        if self._nc is None:
            return

        threshold_s = config.get("long_unhealthy_re_escalate_hours", LONG_UNHEALTHY_RE_ESCALATE_HOURS) * 3600
        realert_s = config.get("long_unhealthy_realert_interval_s", LONG_UNHEALTHY_REALERT_INTERVAL_S)
        local_only_threshold_s = (
            config.get(
                "long_unhealthy_local_only_escalate_hours",
                LONG_UNHEALTHY_LOCAL_ONLY_ESCALATE_HOURS,
            )
            * 3600
        )
        now = time.time()

        for component, state in list(self._component_health.items()):
            # Heartbeat-derived components don't have pods to act on; their own
            # alerting path (CORTEX_STUCK, cortex-rollback) handles the bad cases.
            if component.endswith("-heartbeat"):
                continue

            if state["healthy"]:
                self._long_unhealthy_alerted.pop(component, None)
                continue

            stuck_for = now - state["last_state_change"]
            if stuck_for < threshold_s:
                continue

            # Tiered hive guard (#311): if some other site has this component
            # healthy, the system as a whole is fine and we don't want to wake
            # Adi up for what looks like a local-only outage — but only up to a
            # point. The original "always suppress on healthy peers" policy
            # implicitly assumed *something* else would heal the local instance;
            # for components without an autonomous recovery path (not on the
            # stuck-recovery allowlist, or with a failure mode it doesn't fit)
            # nothing ever does, and the silence becomes the bug. So:
            #
            # - Under ``local_only_threshold_s`` (default 72h): suppress as
            #   before. Matches `_trigger_reflex` / `_check_stuck_components`.
            # - Past it: escalate anyway, but tag the alert and action with
            #   ``LOCAL-ONLY-LONG-UNHEALTHY`` so the policy is legible in the
            #   audit trail and Adi can tell at a glance that this isn't a
            #   system-wide outage — it's a hive dead-zone breakthrough.
            healthy_in_hive = self.component_healthy_in_hive(component)
            local_only_breakthrough = False
            if healthy_in_hive:
                if stuck_for < local_only_threshold_s:
                    continue
                local_only_breakthrough = True

            incident = state["last_state_change"]
            tracker = self._long_unhealthy_alerted.get(component)
            if tracker is None or tracker.get("incident") != incident:
                tracker = {"incident": incident, "last_fired_at": 0.0, "fire_count": 0}
                self._long_unhealthy_alerted[component] = tracker

            last_fired = tracker.get("last_fired_at") or 0.0
            first_fire = last_fired == 0.0
            if not first_fire and (now - last_fired) < realert_s:
                continue

            fire_count = int(tracker.get("fire_count") or 0) + 1
            tracker["last_fired_at"] = now
            tracker["fire_count"] = fire_count

            stuck_hours = round(stuck_for / 3600, 1)
            details = state.get("details", {})
            pod_name = details.get("pod_name", "?")
            phase = details.get("phase")
            waiting_reason = details.get("waiting_reason")
            restarts = details.get("restarts", 0)
            consecutive_failures = state.get("consecutive_failures", 0)

            alert_label = "LOCAL-ONLY-LONG-UNHEALTHY" if local_only_breakthrough else "LONG-UNHEALTHY"

            log.error(
                "Long-unhealthy component — re-escalating",
                extra={
                    "component": component,
                    "pod_name": pod_name,
                    "stuck_for_hours": stuck_hours,
                    "consecutive_failures": consecutive_failures,
                    "phase": phase,
                    "waiting_reason": waiting_reason,
                    "restarts": restarts,
                    "fire_count": fire_count,
                    "alert_label": alert_label,
                    "healthy_in_hive": healthy_in_hive,
                },
            )

            if local_only_breakthrough:
                local_only_note = (
                    f" Hive shows healthy peers on {healthy_in_hive} — this is a "
                    f"local-only outage that has slipped past the normal hive-guard "
                    f"suppression (>{round(local_only_threshold_s / 3600, 1)}h)."
                )
            else:
                local_only_note = ""

            await self._publish_alert(
                f"{alert_label}: {component} ({pod_name}) has been unhealthy for {stuck_hours}h "
                f"(consecutive_failures={consecutive_failures}, phase={phase}, "
                f"waiting_reason={waiting_reason or 'none'}, restarts={restarts}). "
                f"Re-escalation #{fire_count} — initial reflex/escalation did not resolve."
                f"{local_only_note}"
            )

            action = {
                "type": "long_unhealthy_re_escalation",
                "component": component,
                "pod_name": pod_name,
                "stuck_for_hours": stuck_hours,
                "consecutive_failures": consecutive_failures,
                "phase": phase,
                "waiting_reason": waiting_reason,
                "restarts": restarts,
                "fire_count": fire_count,
                "alert_label": alert_label,
                "healthy_in_hive": healthy_in_hive,
                "timestamp": now,
            }
            self._recent_actions.append(action)
            if len(self._recent_actions) > self._recent_actions_max:
                self._recent_actions.pop(0)
            self._schedule_persist()
            try:
                await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
            except Exception:
                log.exception("Failed to publish long-unhealthy re-escalation action")

            # Hand back to Claude for a fresh investigation on every re-escalation —
            # the whole point of this path is that the previous escalation did not
            # resolve the underlying problem, so spawning one Claude turn per
            # ``realert_s`` window is the desired cadence (not just first-fire-only).
            # ``_escalate_to_claude`` is fire-and-forget; if Claude is busy the
            # semaphore upstream will serialise the turn.
            if self._escalate_to_claude is not None:
                local_only_reason = (
                    f" Hive peers {healthy_in_hive} have this component healthy, so the "
                    f"normal hive-guard suppression has been holding back the alert; this "
                    f"escalation breaks through after {round(local_only_threshold_s / 3600, 1)}h "
                    f"because a local-only outage that nothing autonomous can fix is still an "
                    f"outage worth investigating."
                    if local_only_breakthrough
                    else ""
                )
                reason = (
                    f"{alert_label} re-escalation: {component} ({pod_name}) has been unhealthy "
                    f"for {stuck_hours}h (consecutive_failures={consecutive_failures}, "
                    f"phase={phase}, waiting_reason={waiting_reason or 'none'}, restarts={restarts}). "
                    f"This is re-escalation #{fire_count} — earlier reflex/escalation did not fix "
                    f"the root cause.{local_only_reason} Investigate logs/events/dependencies and "
                    f"remediate. If the underlying issue is structural, file or revisit the "
                    f"relevant bug issue."
                )
                spawn_background(
                    self._escalate_to_claude(component, state, reason),
                    name="immune.long_unhealthy_reescalation",
                )

    # --- Immune Self-Health Watchdog ---

    async def _check_immune_self_health(self, config: dict) -> None:
        """Detect when the monitor's own view of a component has gone stale (#270).

        All the other escalation paths in this file trust ``_component_health`` —
        they decide what to do based on ``healthy``, ``consecutive_failures``,
        ``last_state_change`` etc. If the loop that *writes* that state silently
        stops for one component (an unhandled exception in a per-component
        sub-task, an asyncio.gather swallowing an error, a task cancelled and not
        restarted), every downstream check keeps reading stale data and the rest
        of the immune surface can't tell the difference between "component is
        really still in this state" and "we stopped looking days ago".

        This watchdog catches that. If ``now - last_check`` exceeds
        ``SELF_HEALTH_STALE_MULTIPLIER × _check_interval`` for any component, we
        classify it as an immune-self-failure: the monitor itself is the broken
        thing, the component's apparent state is untrustworthy. We alert (with a
        cooldown so a wedged sub-task doesn't fire every tick) so Adi sees it
        quickly, and we record an action so the divergence is visible in the
        hive/recent-actions audit trail.

        Deliberately *only* alerts. No reflex restart of immune itself — if the
        process is half-wedged we don't want it making things worse by hitting
        K8s with delete calls. Recovery of the monitor is left to the kubelet
        liveness probe and to Adi (or a future Claude turn) acting on the alert.
        """
        if self._nc is None:
            return

        multiplier = config.get("self_health_stale_multiplier", SELF_HEALTH_STALE_MULTIPLIER)
        realert_s = config.get("self_health_realert_interval_s", SELF_HEALTH_REALERT_INTERVAL_S)
        threshold_s = max(multiplier * self._check_interval, 1.0)
        now = time.time()

        for component, state in list(self._component_health.items()):
            last_check = state.get("last_check") or 0.0
            if last_check == 0.0:
                # Never been checked yet (e.g., during init). Not stale, just
                # uninitialised; the regular path will fill it in.
                self._self_health_alerted.pop(component, None)
                continue

            stale_for = now - last_check
            if stale_for < threshold_s:
                # Healthy: monitor is actively writing state for this component.
                # Drop any tracker so the next wedge re-arms cleanly.
                self._self_health_alerted.pop(component, None)
                continue

            tracker = self._self_health_alerted.get(component)
            if tracker is None:
                tracker = {
                    "first_seen_stale_at": now,
                    "last_check_when_stale": last_check,
                    "last_fired_at": 0.0,
                    "fire_count": 0,
                }
                self._self_health_alerted[component] = tracker

            last_fired = tracker.get("last_fired_at") or 0.0
            first_fire = last_fired == 0.0
            if not first_fire and (now - last_fired) < realert_s:
                continue

            fire_count = int(tracker.get("fire_count") or 0) + 1
            tracker["last_fired_at"] = now
            tracker["fire_count"] = fire_count

            stale_min = round(stale_for / 60, 1)
            last_known_healthy = state.get("healthy")
            consecutive_failures = state.get("consecutive_failures", 0)

            log.error(
                "Immune self-health: component check is stale",
                extra={
                    "component": component,
                    "stale_for_s": round(stale_for, 1),
                    "threshold_s": round(threshold_s, 1),
                    "last_check_age_s": round(stale_for, 1),
                    "last_known_healthy": last_known_healthy,
                    "consecutive_failures": consecutive_failures,
                    "fire_count": fire_count,
                    "instance_id": self._instance_id,
                },
            )

            await self._publish_alert(
                f"IMMUNE-SELF-FAILURE: monitor stopped updating {component} {stale_min}min ago "
                f"(threshold {round(threshold_s / 60, 1)}min = {multiplier}× poll interval "
                f"{self._check_interval}s). Last-known state: healthy={last_known_healthy}, "
                f"consecutive_failures={consecutive_failures}. Treat that state as untrustworthy "
                f"— a downstream task likely died silently. Alert #{fire_count} from "
                f"instance {self._instance_id}."
            )

            action = {
                "type": "immune_self_failure",
                "component": component,
                "stale_for_s": round(stale_for, 1),
                "threshold_s": round(threshold_s, 1),
                "last_known_healthy": last_known_healthy,
                "consecutive_failures": consecutive_failures,
                "fire_count": fire_count,
                "instance_id": self._instance_id,
                "timestamp": now,
            }
            self._recent_actions.append(action)
            if len(self._recent_actions) > self._recent_actions_max:
                self._recent_actions.pop(0)
            self._schedule_persist()
            try:
                await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
            except Exception:
                log.exception("Failed to publish immune self-failure action")

    # --- Stuck-Component Auto-Recovery (Tier 3) ---

    async def _check_stuck_recovery(self, config: dict) -> None:
        """Automated ``delete pod`` for components stuck initializing past the recovery threshold.

        Plugs the autonomy gap exposed by #264: maki-vault sat PodInitializing for
        8+ days while the hive guard correctly suppressed pages (other sites had
        healthy vault, so "local-only issue"), but "local-only" is not the same as
        "ignore forever" — the broken local instance still needs to recover. The
        restart reflex churns the pod every hour but cannot fix deeper issues
        (stuck PVC binding, image pull failures, init container hangs); after a
        full day of fruitless reflex churn, a fresh ``delete pod`` is usually the
        only safe autonomous move. Kubelet recreates the pod from spec, the PVC
        survives, and the new pod typically re-binds cleanly.

        Distinct from ``_trigger_reflex`` (which fires every few minutes once a
        pod is unhealthy) and ``_check_stuck_components`` (which only escalates
        via alert/Claude). This is the heavier, slower-fire safety net for the
        case both of those have already failed to recover the pod.

        Safety gates (the "more deliberate gate than reflex restart" the issue
        asks for):

        - **Opt-in allowlist** — only components in the allowlist participate.
          Default is ``maki-vault``; other single-replica stateful components
          can be added once we've confirmed ``delete pod`` is safe for them.
        - **Long threshold** — default 24h, far beyond any normal recovery
          window. By this point reflex has tried ~72 times and gotten nowhere.
        - **Hive sanity check** — only act when at least one peer site has the
          component healthy. That proves the recipe (image, config, manifest)
          works, so this really is a local-only issue worth fixing autonomously.
          A system-wide outage (no healthy peers) is left to human judgment.
        - **Cooldown** — once we delete, we don't try again for
          ``stuck_recovery_cooldown_s`` (default 6h), giving the rescheduled
          pod a full window to either recover or re-wedge before another attempt.
        - **Per-incident tracker** — re-arms cleanly when ``last_state_change``
          advances, so a recover-and-fail-again cycle gets a fresh budget.
        """
        if self._nc is None or self._k8s_v1 is None:
            return

        threshold = config.get("stuck_recovery_threshold_s", STUCK_RECOVERY_THRESHOLD_S)
        cooldown = config.get("stuck_recovery_cooldown_s", STUCK_RECOVERY_COOLDOWN_S)
        allowlist_raw = config.get("stuck_recovery_allowlist", STUCK_RECOVERY_ALLOWLIST_DEFAULT)
        allowlist = {c.strip() for c in (allowlist_raw or "").split(",") if c.strip()}

        if not allowlist:
            return

        now = time.time()

        for component, state in list(self._component_health.items()):
            if component not in allowlist:
                continue

            if state["healthy"]:
                self._stuck_recovery_attempts.pop(component, None)
                continue

            details = state.get("details", {})
            phase = details.get("phase")
            waiting_reason = details.get("waiting_reason")

            # Same shape-check as ``_check_stuck_components``: only the never-
            # finishes-initializing / non-Running pod case. A Running+ready=False
            # pod is a different failure mode that should be handled by HTTP-level
            # remediation, not a blind delete (the pod is at least up enough to
            # answer probes — deleting it might destroy in-flight state).
            is_pod_stuck = (phase is not None and phase != "Running") or waiting_reason is not None
            if not is_pod_stuck:
                self._stuck_recovery_attempts.pop(component, None)
                continue

            stuck_for = now - state["last_state_change"]
            if stuck_for < threshold:
                continue

            # Hive sanity check: only act when at least one peer reports healthy.
            # If no peer has this component working, this is either a system-wide
            # outage or a brand-new component nobody's running yet — either way,
            # blindly deleting our local pod is not the right autonomous move.
            # Note the polarity flip vs ``_check_stuck_components``: that path
            # *suppresses* on healthy peers (don't page about local-only issues);
            # this path *requires* healthy peers (only act when we're confident
            # the recipe works elsewhere).
            healthy_peers = self.component_healthy_in_hive(component)
            if not healthy_peers:
                log.warning(
                    "Stuck recovery threshold crossed but no healthy peers — skipping (system-wide issue)",
                    extra={"component": component, "stuck_for_hours": round(stuck_for / 3600, 1)},
                )
                continue

            incident = state["last_state_change"]
            tracker = self._stuck_recovery_attempts.get(component)
            if tracker is None or tracker.get("incident") != incident:
                tracker = {"incident": incident, "last_attempted_at": 0.0, "count": 0}
                self._stuck_recovery_attempts[component] = tracker

            last_attempted = tracker.get("last_attempted_at") or 0.0
            if last_attempted and (now - last_attempted) < cooldown:
                continue

            pod_name = details.get("pod_name")
            if not pod_name:
                continue

            stuck_hours = round(stuck_for / 3600, 1)
            try:
                async with self._infra_lock("immune-stuck-recovery", ttl=60):
                    try:
                        await asyncio.to_thread(
                            self._k8s_v1.delete_namespaced_pod,
                            name=pod_name,
                            namespace=self._namespace,
                            grace_period_seconds=30,
                        )

                        count = int(tracker.get("count") or 0) + 1
                        tracker["last_attempted_at"] = now
                        tracker["count"] = count

                        log.error(
                            "Stuck-recovery pod delete fired",
                            extra={
                                "component": component,
                                "pod_name": pod_name,
                                "stuck_for_hours": stuck_hours,
                                "attempt_count": count,
                                "healthy_peers": healthy_peers,
                                "phase": phase,
                                "waiting_reason": waiting_reason,
                            },
                        )

                        await self._publish_alert(
                            f"AUTO-RECOVERY: deleted {pod_name} after {stuck_hours}h stuck in "
                            f"phase={phase}/{waiting_reason or 'unhealthy'}. Hive shows healthy "
                            f"peers on {healthy_peers} so this is a local-only wedge safe to act on. "
                            f"Attempt #{count}; kubelet will recreate from spec."
                        )

                        action = {
                            "type": "stuck_recovery_delete",
                            "component": component,
                            "pod_name": pod_name,
                            "stuck_for_hours": stuck_hours,
                            "attempt_count": count,
                            "healthy_peers": healthy_peers,
                            "phase": phase,
                            "waiting_reason": waiting_reason,
                            "timestamp": now,
                        }
                        self._recent_actions.append(action)
                        if len(self._recent_actions) > self._recent_actions_max:
                            self._recent_actions.pop(0)
                        self._schedule_persist()
                        try:
                            await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
                        except Exception:
                            log.exception("Failed to publish stuck-recovery action")
                    except Exception:
                        log.exception("Failed to delete stuck pod", extra={"pod_name": pod_name})
            except LockNotAcquired:
                log.warning("Cannot acquire lock for stuck recovery", extra={"component": component})
                continue

    # --- Gossip Ring ---

    async def _refresh_running_images(self) -> None:
        """Query K8s for current image tags on all maki deployments/statefulsets."""
        if not self._k8s_apps_v1:
            return
        images: dict[str, str] = {}
        try:
            deps = await asyncio.to_thread(self._k8s_apps_v1.list_namespaced_deployment, namespace=self._namespace)
            for dep in deps.items:
                name = dep.metadata.name
                if not name.startswith("maki-") or name == "maki-nerve-nats-box":
                    continue
                img = dep.spec.template.spec.containers[0].image
                images[name] = img.rsplit(":", 1)[-1] if ":" in img else "latest"
        except Exception:
            log.exception("Failed to refresh deployment images")

        try:
            sts_list = await asyncio.to_thread(
                self._k8s_apps_v1.list_namespaced_stateful_set, namespace=self._namespace
            )
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
            self._running_images.clear()
            self._running_images.update(images)

    async def gossip_publisher(self) -> None:
        """Broadcast local state to all immune instances via NATS gossip."""
        log.info("Gossip publisher started", extra={"site": self._site_name, "interval": self._check_interval})
        while True:
            try:
                await self._refresh_running_images()
                # Load cortex config (chat_model etc.) for gossip
                try:
                    cortex_config = await load_kv_config(self._cortex_config_kv, {})
                except Exception:
                    cortex_config = {}

                payload = {
                    "site": self._site_name,
                    "instance_id": self._instance_id,
                    "timestamp": time.time(),
                    "component_health": {
                        k: {"healthy": v["healthy"], "consecutive_failures": v["consecutive_failures"]}
                        for k, v in self._component_health.items()
                    },
                    "recent_actions": self._recent_actions[-10:],
                    "cortex": {
                        "last_heartbeat_age_s": round(time.time() - self._cortex_state["last_heartbeat"], 1)
                        if self._cortex_state["last_heartbeat"]
                        else None,
                        "active_turn": self._cortex_state["active_turn"],
                        "turn_mode": self._cortex_state["turn_mode"],
                    },
                    "cortex_config": cortex_config,
                    "token_usage_today": {
                        "date": self._token_stats["date"],
                        "total_tokens": self._token_stats["total_tokens"],
                        "total_cost_usd": round(self._token_stats["total_cost_usd"], 4),
                        "turns": self._token_stats["turns"],
                        "by_model": self._token_stats["by_model"],
                    },
                    "blacklist": list(self._failed_image_blacklist),
                    "images": dict(self._running_images),
                }
                await self._nc.publish(IMMUNE_HEALTH, json.dumps(payload).encode())
            except Exception:
                log.exception("Gossip publish failed")
            await asyncio.sleep(self._check_interval)

    async def _handle_gossip(self, msg) -> None:
        payload = json.loads(msg.data.decode())
        site = payload.get("site", "unknown")
        if site == self._site_name:
            return

        was_new = site not in self._hive_state
        self._hive_state[site] = {**payload, "received_at": time.time()}

        if was_new:
            log.info("Peer joined hive", extra={"site": site, "instance_id": payload.get("instance_id")})

        now = time.time()
        stale = [s for s, v in self._hive_state.items() if now - v["received_at"] > self._gossip_stale_threshold]
        for s in stale:
            log.warning("Peer went silent, pruning", extra={"site": s})
            del self._hive_state[s]

    async def gossip_listener(self) -> None:
        """Subscribe to gossip from all immune instances, build hive-wide state."""
        await subscribe_supervised(
            self._nc,
            IMMUNE_HEALTH,
            self._handle_gossip,
            # Pure in-memory dict mutation + stale-peer prune. Ten seconds
            # is generous. Bounding this matters: gossip is what feeds the
            # hive-wide state view — a wedged handler would blind this
            # instance to every peer's health (#492).
            handler_timeout=10.0,
            name="gossip",
        )
