"""Claude reasoning, escalation, and immune heartbeat for maki-immune."""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from maki_common import DEFAULT_CLAUDE_MODEL, load_kv_config, parse_config_tags, spawn_background
from maki_common.claude import invoke_claude
from maki_common.config import apply_config_updates, parse_tagged
from maki_common.nats import try_claim_loop
from maki_common.subjects import IMMUNE_ACTION

log = logging.getLogger(__name__)

MAX_CLAUDE_TURNS = int(os.environ.get("IMMUNE_MAX_TURNS", "8"))
MODEL = os.environ.get("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)

# Set by init()
_nc: Any = None
_namespace: str = ""
_instance_id: str = ""
_site_name: str = ""
_check_interval: int = 30
_component_health: Any = None
_pod_metrics: Any = None
_recent_actions: Any = None
_recent_actions_max: int = 100
_running_images: Any = None
_hive_state: Any = None
_cortex_state: Any = None
_failed_image_blacklist: Any = None
_config_kv: Any = None
_default_config: dict[str, Any] = {}
_config_validators: dict[str, list] = {}
_mcp_server: Any = None
_semaphore: Any = None
_system_prompt: str = ""
_publish_alert: Any = None
_publish_vitals: Any = None
_publish_immune_response: Any = None
_schedule_persist: Any = None
_k8s_v1: Any = None
_lock_kv: Any = None
_loop_heartbeats: dict[str, float] = {}  # name -> last successful run timestamp

# Loop heartbeats older than this are considered retired/renamed and are
# actively pruned from both the in-memory cache and the shared ``maki-lock``
# KV bucket by ``loop_heartbeat_watcher`` (see #161). The threshold sits well
# above the slowest loop cadence (the ``work`` loop runs daily) so a
# legitimately quiet loop is never evicted mid-cycle.
_LOOP_HEARTBEAT_TTL_S = 7 * 24 * 3600  # 7 days

# How many recent escalation rows to render into the Claude system prompt. The
# durable trail itself lives in _recent_actions (persisted to KV, gossiped via
# hive_state) — this just caps the rendered slice.
_ESCALATION_PROMPT_WINDOW = 10
# Truncate Claude's response / the exception message before stuffing it into
# the durable action record so the KV blob stays bounded.
_ESCALATION_SUMMARY_MAX = 400

# Error signatures for passive log monitoring
_ERROR_SIGNATURES = re.compile(r"ERROR|EXCEPTION|Traceback|panic|FATAL|RuntimeError|CrashLoopBackOff")


def init(
    *,
    nc,
    namespace,
    instance_id,
    site_name,
    check_interval,
    component_health,
    pod_metrics,
    recent_actions,
    running_images,
    hive_state,
    cortex_state,
    failed_image_blacklist,
    config_kv,
    default_config,
    config_validators,
    mcp_server,
    semaphore,
    system_prompt,
    publish_alert,
    publish_vitals,
    publish_immune_response,
    recent_actions_max=100,
    schedule_persist_recent_actions=None,
    k8s_v1=None,
    lock_kv=None,
):
    global _nc, _namespace, _instance_id, _site_name, _check_interval
    global _component_health, _pod_metrics, _recent_actions, _recent_actions_max
    global _running_images, _hive_state
    global _cortex_state, _failed_image_blacklist
    global _config_kv, _default_config, _config_validators, _mcp_server, _semaphore, _system_prompt
    global _publish_alert, _publish_vitals, _publish_immune_response, _schedule_persist
    global _k8s_v1, _lock_kv
    _nc = nc
    _namespace = namespace
    _instance_id = instance_id
    _site_name = site_name
    _check_interval = check_interval
    _component_health = component_health
    _pod_metrics = pod_metrics
    _recent_actions = recent_actions
    _recent_actions_max = recent_actions_max
    _running_images = running_images
    _hive_state = hive_state
    _cortex_state = cortex_state
    _failed_image_blacklist = failed_image_blacklist
    _config_kv = config_kv
    _default_config = default_config
    _config_validators = config_validators
    _mcp_server = mcp_server
    _semaphore = semaphore
    _system_prompt = system_prompt
    _publish_alert = publish_alert
    _publish_vitals = publish_vitals
    _publish_immune_response = publish_immune_response
    _schedule_persist = schedule_persist_recent_actions
    _k8s_v1 = k8s_v1
    _lock_kv = lock_kv


# --- System State Builder ---


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

        pod_name = details.get("pod_name", "")
        if pod_name and pod_name in _pod_metrics:
            m = _pod_metrics[pod_name]
            parts.append(f"cpu_usage={m.get('cpu', '?')}")
            parts.append(f"mem_usage={m.get('memory', '?')}")

        if component == "maki-cortex-heartbeat" and details.get("active_turn"):
            parts.append(f"active_turn={details['active_turn']}")
            parts.append(f"mode={details.get('turn_mode', '?')}")
            turn_running = details.get("turn_running_s")
            if turn_running is not None:
                parts.append(f"turn_running={round(turn_running / 60, 1)}min")

        lines.append(f"- {component}: {status} ({', '.join(parts)})")

    if not lines:
        lines.append("No health data collected yet.")

    if _running_images:
        lines.append(f"\n## Local Images ({_site_name})")
        for dep, tag in sorted(_running_images.items()):
            lines.append(f"- {dep}: {tag}")

    if _hive_state:
        lines.append(f"\n## Hive State ({len(_hive_state)} peer(s) connected, local site: {_site_name})")
        for site, state in sorted(_hive_state.items()):
            peer_health = state.get("component_health", {})
            total = len(peer_health)
            healthy = sum(1 for v in peer_health.values() if v.get("healthy"))
            cortex_info = state.get("cortex", {})
            cortex_age = cortex_info.get("last_heartbeat_age_s")
            if cortex_age is not None and cortex_age < 60:
                turn = cortex_info.get("active_turn")
                cortex_str = f"cortex active (turn {turn})" if turn else "cortex idle"
            elif cortex_age is not None:
                cortex_str = f"cortex DOWN (heartbeat {round(cortex_age)}s ago)"
            else:
                cortex_str = "cortex unknown"
            freshness = round(time.time() - state.get("received_at", 0))
            lines.append(f"- {site}: {healthy}/{total} healthy, {cortex_str}, last seen {freshness}s ago")

            peer_images = state.get("images", {})
            if peer_images:
                for dep, tag in sorted(peer_images.items()):
                    local_tag = _running_images.get(dep)
                    drift = f" ⚠ DRIFT (local={local_tag})" if local_tag and local_tag != tag else ""
                    lines.append(f"  - {dep}: {tag}{drift}")
    else:
        lines.append(f"\n## Hive State (no peers connected, local site: {_site_name})")

    if _loop_heartbeats:
        lines.append("\n## Loop Heartbeats")
        now = time.time()
        for name, ts in sorted(_loop_heartbeats.items()):
            age_h = round((now - ts) / 3600, 1)
            lines.append(f"- loop.{name}: last ran {age_h}h ago")
    else:
        lines.append("\n## Loop Heartbeats\n- No heartbeats recorded yet (loops haven't fired or pod restarted)")

    escalation_block = _render_recent_escalations()
    if escalation_block:
        lines.append("\n" + escalation_block)

    return "\n".join(lines)


def _render_recent_escalations() -> str:
    """Render the recent Claude-escalation trail for the system-state prompt (#299).

    The full event stream lives in ``_recent_actions`` (persisted to KV +
    gossiped via hive_state). Here we just filter for the escalation entries
    and pair started/finished rows so Claude can see, at a glance, whether the
    last N escalations actually completed or vanished into ``log.exception``.
    A run of "started" rows with no matching "complete"/"failed" is the exact
    smoking gun the #299 incident exposed.
    """
    if not _recent_actions:
        return ""

    escalation_types = {
        "claude_escalation_started",
        "claude_escalation_complete",
        "claude_escalation_failed",
    }
    escalations = [a for a in _recent_actions if a.get("type") in escalation_types]
    if not escalations:
        return ""

    escalations = escalations[-_ESCALATION_PROMPT_WINDOW:]
    now = time.time()
    lines = [f"## Recent Claude Escalations (last {len(escalations)})"]
    for action in escalations:
        kind = action.get("type", "")
        component = action.get("component", "?")
        ts = action.get("timestamp") or 0.0
        age_min = round((now - ts) / 60, 1) if ts else "?"
        eid = action.get("escalation_id", "")
        eid_str = f" id={eid}" if eid else ""

        if kind == "claude_escalation_started":
            reason = (action.get("reason") or "")[:160]
            lines.append(f"- STARTED {component}{eid_str} ({age_min}min ago): {reason}")
        elif kind == "claude_escalation_complete":
            summary = (action.get("summary") or "")[:160]
            lines.append(f"- COMPLETE {component}{eid_str} ({age_min}min ago): {summary}")
        elif kind == "claude_escalation_failed":
            err = (action.get("error") or "")[:160]
            lines.append(f"- FAILED {component}{eid_str} ({age_min}min ago): {err}")

    return "\n".join(lines)


def _record_action(action: dict) -> None:
    """Append a structured action to _recent_actions and persist (#299).

    Mirrors the pattern in health.py: append, trim, schedule persist, publish
    to ``IMMUNE_ACTION`` so hive peers see it too. Wrapped here so the claude
    module doesn't have to know about the persistence wiring at every call
    site.
    """
    if _recent_actions is None:
        return
    _recent_actions.append(action)
    if len(_recent_actions) > _recent_actions_max:
        _recent_actions.pop(0)
    if _schedule_persist is not None:
        try:
            _schedule_persist()
        except Exception:
            log.exception("Failed to schedule recent_actions persist")
    if _nc is not None:
        try:
            spawn_background(
                _nc.publish(IMMUNE_ACTION, json.dumps(action, default=str).encode()),
                name="immune.action_publish",
            )
        except Exception:
            log.exception("Failed to publish escalation action")


# --- Shared Claude Invocation ---


async def _invoke_immune_claude(prompt_suffix: str = "") -> str:
    """Assemble the standard immune prompt, invoke Claude, apply config-tag updates,
    publish any ``[ALERT:...]`` tags, and return the raw response text.

    Centralizes the prompt-assembly + invoke_claude + parse_config_tags +
    apply_config_updates + ALERT-publish boilerplate shared by
    ``escalate_to_claude``, ``handle_immune_command``, and
    ``immune_heartbeat_loop``. Callers handle their own divergent reporting
    (DIGEST / RESPONSE / [SILENT]) on the returned text.

    Note: ALERTs are published from this single site so the three call sites
    cannot drift. DIGESTs are intentionally caller-controlled — escalate sends
    them to #maki-vitals, the heartbeat loop suppresses them when [SILENT] is
    set, and the command handler suppresses them outright (#maki-immune
    already gets the full RESPONSE).
    """
    system_state = _build_system_state()
    recent_actions_str = json.dumps(_recent_actions[-10:], indent=2, default=str) if _recent_actions else "None"
    config = await load_kv_config(_config_kv, _default_config)

    prompt = _system_prompt.format(
        system_state=system_state,
        recent_actions=recent_actions_str,
        config=json.dumps(config, indent=2),
    )
    if prompt_suffix:
        prompt += prompt_suffix

    response, _usage = await invoke_claude(
        prompt,
        model=MODEL,
        semaphore=_semaphore,
        max_turns=MAX_CLAUDE_TURNS,
        mcp_servers={"maki-immune": _mcp_server},
    )

    config_updates = parse_config_tags(response)
    await apply_config_updates(
        _config_kv,
        config_updates,
        allowed_keys=set(_default_config.keys()),
        validators=_config_validators,
    )

    for alert in parse_tagged(response, "ALERT"):
        await _publish_alert(alert)

    return response


# --- Claude Escalation ---


async def escalate_to_claude(component: str, state: dict, reason: str):
    """Escalate a problem to Claude for deeper investigation and remediation.

    Durability contract (#299): every call leaves at least two structured
    entries in ``_recent_actions`` — a ``claude_escalation_started`` row
    written *before* the first ``await``, and a ``claude_escalation_complete``
    or ``claude_escalation_failed`` row written in ``finally``. Even if the
    LLM call hangs, errors, or the MCP tools silently no-op, the trail is
    guaranteed and is surfaced in ``_build_system_state`` so the next
    reflection/escalation can see the pattern (e.g., "we've tried 38 times,
    every one failed at the same step").
    """
    started_at = time.time()
    escalation_id = f"{component}:{int(started_at)}"
    log.info(
        "Escalating to Claude",
        extra={"component": component, "reason": reason, "escalation_id": escalation_id},
    )

    # Durable start record — written before any await so even if everything
    # below explodes, the attempt is visible in the action trail / hive state.
    _record_action(
        {
            "type": "claude_escalation_started",
            "component": component,
            "escalation_id": escalation_id,
            "reason": reason[:_ESCALATION_SUMMARY_MAX],
            "timestamp": started_at,
            "instance_id": _instance_id,
            "site": _site_name,
        }
    )

    outcome: dict[str, Any] = {
        "type": "claude_escalation_failed",
        "component": component,
        "escalation_id": escalation_id,
        "started_at": started_at,
        "error": "unknown — neither success nor exception was recorded",
        "instance_id": _instance_id,
        "site": _site_name,
    }

    try:
        suffix = f"""

## ESCALATION

The fast reflex loop has escalated {component} to you because: {reason}

Component details: {json.dumps(state, default=str)}

Investigate this problem using your tools. Read logs, check events, examine the pod.
Determine root cause and take corrective action if possible.
Always report what you found and what you did via [DIGEST:...] and/or [ALERT:...]."""

        response = await _invoke_immune_claude(suffix)

        digests = parse_tagged(response, "DIGEST")
        for digest in digests:
            await _publish_vitals(digest)

        # ALERTs already published inside _invoke_immune_claude; re-parse here
        # only so the durable outcome record can count/include them in summary.
        alerts = parse_tagged(response, "ALERT")

        summary_source = "\n".join(digests + alerts) if (digests or alerts) else response
        outcome = {
            "type": "claude_escalation_complete",
            "component": component,
            "escalation_id": escalation_id,
            "started_at": started_at,
            "duration_s": round(time.time() - started_at, 1),
            "summary": (summary_source or "").strip()[:_ESCALATION_SUMMARY_MAX],
            "digests": len(digests),
            "alerts": len(alerts),
            "instance_id": _instance_id,
            "site": _site_name,
        }
        log.info(
            "Claude escalation complete",
            extra={
                "component": component,
                "escalation_id": escalation_id,
                "duration_s": outcome["duration_s"],
                "digests": len(digests),
                "alerts": len(alerts),
            },
        )

    except Exception as exc:
        err_summary = f"{type(exc).__name__}: {exc}"[:_ESCALATION_SUMMARY_MAX]
        outcome = {
            "type": "claude_escalation_failed",
            "component": component,
            "escalation_id": escalation_id,
            "started_at": started_at,
            "duration_s": round(time.time() - started_at, 1),
            "error": err_summary,
            "instance_id": _instance_id,
            "site": _site_name,
        }
        log.exception(
            "Claude escalation failed",
            extra={"component": component, "escalation_id": escalation_id},
        )
        # The whole point of #299: when the LLM path explodes, the *failure*
        # itself becomes the alert. Otherwise the escalation pipeline can sit
        # silently broken (as it did here for 9.5 days). Wrapped in its own
        # try/except so a publish failure can't suppress the durable record.
        try:
            if _publish_alert is not None:
                await _publish_alert(
                    f"ESCALATION-FAILED: claude escalation for {component} crashed "
                    f"({err_summary}). Reason was: {reason[:160]}. "
                    f"escalation_id={escalation_id}. No remediation ran — manual review needed."
                )
        except Exception:
            log.exception("Failed to publish escalation-failed alert")

    finally:
        _record_action(outcome)


# --- Immune Command Handler ---


async def handle_immune_command(msg):
    """Handle direct commands from Adi via #maki-immune Discord channel."""
    try:
        payload = json.loads(msg.data.decode())
        message_id = payload.get("message_id", "")
        command = payload.get("command", "")
        username = payload.get("username", "unknown")

        log.info(
            "Immune command received", extra={"message_id": message_id, "command": command[:100], "username": username}
        )

        # Handle blacklist management commands directly (no Claude needed)
        cmd_lower = command.strip().lower()
        if cmd_lower in ("clear-blacklist", "clear blacklist"):
            cleared = list(_failed_image_blacklist)
            _failed_image_blacklist.clear()
            reply = f"Blacklist cleared. Removed {len(cleared)} entr{'y' if len(cleared) == 1 else 'ies'}: " + (
                ", ".join(cleared) if cleared else "none"
            )
            log.info("Failed-image blacklist cleared via immune command", extra={"cleared": cleared})
            await _publish_immune_response(message_id, reply)
            return
        if cmd_lower in ("show-blacklist", "show blacklist", "list-blacklist", "list blacklist"):
            if _failed_image_blacklist:
                reply = f"Blacklisted image tags ({len(_failed_image_blacklist)}): " + ", ".join(
                    sorted(_failed_image_blacklist)
                )
            else:
                reply = "Blacklist is empty."
            await _publish_immune_response(message_id, reply)
            return

        suffix = f"""

## DIRECT COMMAND FROM ADI

Adi is talking to you directly through the #maki-immune backdoor channel.
This means cortex may be down or unresponsive. Treat this as highest priority.

Adi says: {command}

Investigate and act on this command. Use your tools — read logs, check pods, restart things,
whatever is needed. Respond with a clear summary of what you found and what you did.

Put your full response in [RESPONSE:...] tags. This will be sent back to Adi in Discord.
Also use [DIGEST:...] for anything that should go to #maki-vitals."""

        try:
            # ALERTs are published inside _invoke_immune_claude. DIGEST tags are
            # intentionally not published to #maki-vitals here — the full response
            # already goes back to #maki-immune, and publishing DIGESTs would cause
            # immune updates to leak into #maki-general.
            response = await _invoke_immune_claude(suffix)

            responses = parse_tagged(response, "RESPONSE")
            reply = "\n\n".join(responses) if responses else response

            await _publish_immune_response(message_id, reply)

            log.info("Immune command handled", extra={"message_id": message_id})

        except Exception:
            log.exception("Immune command Claude invocation failed")
            await _publish_immune_response(
                message_id, "Failed to process command — Claude invocation error. Check immune logs."
            )

    except Exception:
        log.exception("Immune command handler error")


# --- Loop Heartbeat Watcher ---


async def loop_heartbeat_watcher() -> None:
    """Poll the shared lock KV for loop heartbeat timestamps and cache them locally.

    Stem writes loop.heartbeat.{name} after each successful body execution.
    We read these periodically so _build_system_state can surface loop health
    to Claude without blocking on async KV reads inside the sync state builder.

    The cache is rebuilt from scratch each cycle (atomic swap) so renamed or
    retired loops don't linger forever in the prompt Claude reads on every
    escalation. Entries older than ``_LOOP_HEARTBEAT_TTL_S`` are additionally
    deleted from the underlying KV — the ``maki-lock`` bucket has no
    bucket-level TTL (it also holds long-lived trading state), so we prune
    here. See #161.
    """
    global _loop_heartbeats
    log.info("Loop heartbeat watcher started")
    while True:
        await asyncio.sleep(60)
        if _lock_kv is None:
            continue
        now = time.time()
        new_heartbeats: dict[str, float] = {}
        try:
            keys = await _lock_kv.keys()
        except Exception:
            # No keys / bucket transient error — leave prior snapshot in place.
            continue
        for key in keys or []:
            if not key.startswith("loop.heartbeat."):
                continue
            name = key.removeprefix("loop.heartbeat.")
            try:
                entry = await _lock_kv.get(key)
                ts = float(entry.value.decode())
            except Exception:
                # Skip this key this cycle; it'll be reconsidered next round.
                continue
            if now - ts > _LOOP_HEARTBEAT_TTL_S:
                # Retired/renamed loop — evict from KV so it stops polluting
                # future reads, and exclude it from this cycle's cache.
                try:
                    await _lock_kv.delete(key)
                    log.info(
                        "Evicted stale loop heartbeat",
                        extra={"loop": name, "age_s": round(now - ts)},
                    )
                except Exception:
                    log.warning(
                        "Failed to delete stale loop heartbeat",
                        extra={"loop": name},
                    )
                continue
            new_heartbeats[name] = ts
        _loop_heartbeats = new_heartbeats


# --- Immune Heartbeat Loop ---


async def immune_heartbeat_loop():
    """Periodic holistic patrol with Claude reasoning."""
    log.info("Immune heartbeat loop started", extra={"instance_id": _instance_id})
    last_patrol = time.time()

    while True:
        await asyncio.sleep(_check_interval)

        try:
            config = await load_kv_config(_config_kv, _default_config)
            interval = config.get("heartbeat_interval", 1800)

            if time.time() - last_patrol < interval:
                continue

            log.info("Immune heartbeat triggered — starting patrol")
            last_patrol = time.time()

            # ALERTs are published inside _invoke_immune_claude.
            response = await _invoke_immune_claude()

            if "[SILENT]" not in response:
                for digest in parse_tagged(response, "DIGEST"):
                    await _publish_vitals(digest)

            log.info("Immune heartbeat complete", extra={"silent": "[SILENT]" in response})

        except Exception:
            log.exception("Immune heartbeat error")


# --- Pattern Escalation ---

_MAX_PATTERN_ESCALATION_TURNS = 4


def _parse_pattern_classification(response: str) -> dict | None:
    """Parse the structured PATTERN_CLASSIFICATION block from Claude's response.

    Returns a dict with keys: component, pattern, classification, confidence, notes
    or None if the block is missing or malformed.
    """
    match = re.search(
        r"PATTERN_CLASSIFICATION:\s*\n"
        r"\s*component:\s*(.+)\n"
        r"\s*pattern:\s*(.+)\n"
        r"\s*classification:\s*(no_op|escalate)\s*\n"
        r"\s*confidence:\s*([0-9.]+)\s*\n"
        r"\s*notes:\s*(.+)",
        response,
        re.IGNORECASE,
    )
    if not match:
        return None

    try:
        confidence = float(match.group(4))
        if not 0.0 <= confidence <= 1.0:
            return None
        return {
            "component": match.group(1).strip(),
            "pattern": match.group(2).strip(),
            "classification": match.group(3).strip().lower(),
            "confidence": confidence,
            "notes": match.group(5).strip(),
        }
    except (ValueError, IndexError):
        return None


def _build_escalation_summary(candidate: dict, classification: dict | None) -> str:
    """Build a brief summary for #maki-immune after pattern escalation."""
    component = candidate["component"]
    pod = candidate["pod"]
    source = candidate.get("source", "current")
    source_suffix = " [crashed instance]" if source == "previous" else ""

    if classification:
        cls = classification["classification"]
        conf = classification["confidence"]
        notes = classification["notes"]
        pat = classification["pattern"]
        return (
            f"**Pattern classified** — {component} ({pod}){source_suffix}\n"
            f"classification: `{cls}` (confidence: {conf:.1f})\n"
            f"pattern: `{pat}`\n"
            f"notes: {notes}"
        )
    else:
        sigs = ", ".join(candidate.get("signatures", []))
        return (
            f"**Pattern escalation** — {component} ({pod}){source_suffix}: "
            f"Claude did not produce a valid PATTERN_CLASSIFICATION block. Manual review needed.\n"
            f"Signatures: {sigs}"
        )


async def escalate_pattern_to_claude(candidate: dict) -> None:
    """Escalate an unknown/untrusted error pattern to Claude for immediate classification.

    Claude investigates the log tail, classifies the pattern, and immune writes the result
    back to error_patterns. At most once per pattern per passive loop window (caller's
    responsibility to enforce via _escalated_this_window).
    """
    from maki_immune.patterns import query_patterns, write_pattern

    component = candidate["component"]
    pod = candidate["pod"]
    log_tail = candidate.get("log_tail", "")
    signatures = candidate.get("signatures", [])
    source = candidate.get("source", "current")
    source_label = (
        "previous container instance (post-crash trace from the terminated container — pod has restart_count > 0)"
        if source == "previous"
        else "current running container (steady-state logs)"
    )

    log.info(
        "Escalating pattern to Claude",
        extra={"component": component, "pod": pod, "signatures": signatures, "source": source},
    )

    # Fetch last 5 known patterns for context (dedup guard)
    known_patterns = await query_patterns(_nc, component)
    if known_patterns:
        known_context = "\n".join(
            f"- pattern={p['pattern']!r} classification={p['classification']} "
            f"confidence={p['confidence']:.1f} notes={p.get('notes', '')!r}"
            for p in known_patterns[-5:]
        )
    else:
        known_context = "(none — first pattern for this component)"

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    prompt = f"""You are the pattern classifier for maki-immune.

An unknown error signature was detected in pod logs. Classify it: is it a safe no-op, or does it require action?

## Observed Error

- Component: {component}
- Pod: {pod}
- Site: {_site_name}
- Signatures found: {", ".join(signatures)}
- Log source: {source_label}
- Timestamp: {timestamp}

## Log tail (raw)
```
{log_tail}
```

## Known patterns for {component} (last 5, for deduplication)
{known_context}

## Your task

1. Investigate if needed — use get_pod_logs or get_k8s_events for this pod if the tail isn't enough.
   For crash-restart cases, get_pod_logs supports previous=true to pull the terminated container's logs.
2. Search memories — you may have seen this before.
3. Act if needed — restart the pod/deployment only if the problem is active and actionable.
4. End your response with a PATTERN_CLASSIFICATION block (required):

PATTERN_CLASSIFICATION:
  component: {component}
  pattern: <concise regex that matches this error>
  classification: no_op | escalate
  confidence: <0.1 to 1.0>
  notes: <one sentence — what this error means and why it's safe or dangerous>

Rules:
- The block is mandatory. If missing, immune treats the pattern as permanently unknown.
- Do not duplicate an existing known pattern — if one already covers this, omit the block entirely.
- Be conservative: when in doubt, classify as escalate rather than no_op."""

    try:
        response, _usage = await invoke_claude(
            prompt,
            model=MODEL,
            semaphore=_semaphore,
            max_turns=_MAX_PATTERN_ESCALATION_TURNS,
            mcp_servers={"maki-immune": _mcp_server},
        )

        classification = _parse_pattern_classification(response)

        if classification:
            await write_pattern(_nc, classification)
            log.info(
                "Pattern classification written",
                extra={
                    "component": component,
                    "pattern": classification.get("pattern"),
                    "classification": classification.get("classification"),
                    "confidence": classification.get("confidence"),
                },
            )
        else:
            log.warning(
                "Claude did not produce a valid PATTERN_CLASSIFICATION block",
                extra={"component": component, "pod": pod},
            )

        summary = _build_escalation_summary(candidate, classification)
        await _publish_alert(summary)

    except Exception:
        log.exception("Pattern escalation to Claude failed", extra={"component": component})


# --- Passive Log Monitor Loop ---


async def passive_log_monitor_loop():
    """Passive log monitor — tails recent pod logs on a fixed cadence, scanning for error signatures.

    When error candidates are found, checks them against known patterns in the
    error_patterns table. Trusted no-op patterns are silently suppressed;
    unknown or untrusted patterns are escalated to Claude.
    """
    log.info("Passive log monitor loop started", extra={"instance_id": _instance_id})

    while True:
        await asyncio.sleep(_check_interval)

        try:
            config = await load_kv_config(_config_kv, _default_config)
            interval = config.get("passive_patrol_interval_seconds", 2700)
            lock_ttl = max(interval - 60, 60)

            # Distributed lock to prevent double-fire across instances
            lock_key = f"loop.immune.passive_patrol.{_site_name}"
            if not await try_claim_loop(_lock_kv, lock_key, lock_ttl, _instance_id):
                continue

            if not _k8s_v1:
                log.debug("Passive patrol skipped — no K8s client")
                continue

            log.info("Passive log patrol triggered")

            # List all pods in namespace
            pods = await asyncio.to_thread(_k8s_v1.list_namespaced_pod, namespace=_namespace)

            candidates: list[dict] = []

            for pod in pods.items:
                app_label = (pod.metadata.labels or {}).get("app", "")
                if not app_label:
                    continue

                pod_name = pod.metadata.name

                # Decide whether the previous container instance is worth reading.
                # If a pod just crashed and k8s restarted it, the current container
                # is the post-restart startup — the actual stack trace lives in
                # `previous` logs. Without this, crash-loop and quick-recovery
                # scenarios are systematically invisible to signature matching
                # (issue #366).
                fetch_previous = False
                if pod.status and pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        restart_count = cs.restart_count or 0
                        had_terminated = bool(cs.last_state and cs.last_state.terminated)
                        if restart_count > 0 or had_terminated:
                            fetch_previous = True
                            break

                # Current container logs.
                try:
                    current_text = await asyncio.to_thread(
                        _k8s_v1.read_namespaced_pod_log,
                        name=pod_name,
                        namespace=_namespace,
                        tail_lines=15,
                    )
                except Exception:
                    current_text = ""

                # Previous (crashed) container logs — only when the pod has
                # actually restarted. Silently swallow the 400 BadRequest that
                # k8s returns when there's no terminated instance to read from,
                # or its logs were already GC'd by kubelet.
                previous_text = ""
                if fetch_previous:
                    try:
                        previous_text = await asyncio.to_thread(
                            _k8s_v1.read_namespaced_pod_log,
                            name=pod_name,
                            namespace=_namespace,
                            tail_lines=15,
                            previous=True,
                        )
                    except Exception:
                        previous_text = ""

                # Emit one candidate per source that had a signature hit. Tagging
                # candidates with their source keeps escalation prompts honest
                # about which container instance actually produced the trace.
                for source, text in (("current", current_text), ("previous", previous_text)):
                    if not text:
                        continue
                    matches = _ERROR_SIGNATURES.findall(text)
                    if not matches:
                        continue
                    candidates.append(
                        {
                            "component": app_label,
                            "pod": pod_name,
                            "signatures": sorted(set(matches)),
                            "log_tail": text.strip()[-500:],
                            "source": source,
                        }
                    )

            if candidates:
                log.info(
                    "Passive patrol found error candidates",
                    extra={
                        "candidate_count": len(candidates),
                        "components": [c["component"] for c in candidates],
                    },
                )

                # Pattern matching: check candidates against known patterns
                from maki_immune.patterns import check_candidates

                suppressed, to_escalate = await check_candidates(_nc, candidates)

                if suppressed:
                    log.info(
                        "Suppressed known no-op patterns",
                        extra={
                            "count": len(suppressed),
                            "components": [c["component"] for c in suppressed],
                        },
                    )

                # Escalate unknowns/untrusted to Claude — at most once per signature
                # fingerprint per passive window to avoid flooding Claude with the same error
                escalated_this_window: set[str] = set()
                for c in to_escalate:
                    # Fingerprint includes source so a previous-container crash
                    # and a current-container error with the same signatures
                    # aren't collapsed — they're different failure modes.
                    source_tag = c.get("source", "current")
                    fingerprint = f"{c['component']}:{source_tag}:{':'.join(sorted(c.get('signatures', [])))}"
                    log.info(
                        "Escalating error candidate",
                        extra={
                            "component": c["component"],
                            "pod": c["pod"],
                            "signatures": c["signatures"],
                            "reason": c.get("reason", "unknown"),
                            "source": source_tag,
                        },
                    )
                    if fingerprint in escalated_this_window:
                        log.debug(
                            "Skipping duplicate pattern escalation this window",
                            extra={"component": c["component"], "fingerprint": fingerprint},
                        )
                        continue
                    escalated_this_window.add(fingerprint)
                    spawn_background(escalate_pattern_to_claude(c), name="immune.pattern_escalation")
            # No-error runs are fully silent — no logging

        except Exception:
            log.exception("Passive log monitor error")


# --- Cortex Stuck Handler ---


async def cortex_stuck_handler(msg):
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
            "cortex_heartbeat_age_s": round(time.time() - _cortex_state["last_heartbeat"], 1)
            if _cortex_state["last_heartbeat"]
            else None,
        }
        reason = f"Cortex turn {turn_id} (mode={mode}) timed out after {timeout_s}s" + (
            ". User is waiting for a response." if user_waiting else "."
        )

        spawn_background(
            escalate_to_claude("maki-cortex", state, reason),
            name=f"immune.cortex_stuck_escalation.{turn_id}",
        )

    except Exception:
        log.exception("Cortex stuck handler error")
