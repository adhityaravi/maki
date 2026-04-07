"""Claude reasoning, escalation, and immune heartbeat for maki-immune."""

import asyncio
import json
import logging
import os
import time
from typing import Any

from maki_common import load_kv_config, parse_config_tags
from maki_common.claude import invoke_claude
from maki_common.config import apply_config_updates, parse_tagged

log = logging.getLogger(__name__)

MAX_CLAUDE_TURNS = int(os.environ.get("IMMUNE_MAX_TURNS", "8"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# Set by init()
_nc: Any = None
_namespace: str = ""
_instance_id: str = ""
_site_name: str = ""
_check_interval: int = 30
_component_health: Any = None
_pod_metrics: Any = None
_recent_actions: Any = None
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
):
    global _nc, _namespace, _instance_id, _site_name, _check_interval
    global _component_health, _pod_metrics, _recent_actions, _running_images, _hive_state
    global _cortex_state, _failed_image_blacklist
    global _config_kv, _default_config, _config_validators, _mcp_server, _semaphore, _system_prompt
    global _publish_alert, _publish_vitals, _publish_immune_response
    _nc = nc
    _namespace = namespace
    _instance_id = instance_id
    _site_name = site_name
    _check_interval = check_interval
    _component_health = component_health
    _pod_metrics = pod_metrics
    _recent_actions = recent_actions
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

    return "\n".join(lines)


# --- Claude Escalation ---


async def escalate_to_claude(component: str, state: dict, reason: str):
    """Escalate a problem to Claude for deeper investigation and remediation."""
    log.info("Escalating to Claude", extra={"component": component, "reason": reason})

    system_state = _build_system_state()
    recent_actions_str = json.dumps(_recent_actions[-10:], indent=2, default=str) if _recent_actions else "None"
    config = await load_kv_config(_config_kv, _default_config)
    config_str = json.dumps(config, indent=2)

    prompt = _system_prompt.format(
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
        response, _usage = await invoke_claude(
            prompt,
            model=MODEL,
            semaphore=_semaphore,
            max_turns=MAX_CLAUDE_TURNS,
            mcp_servers={"maki-immune": _mcp_server},
        )

        config_updates = parse_config_tags(response)
        await apply_config_updates(
            _config_kv, config_updates, allowed_keys=set(_default_config.keys()), validators=_config_validators
        )

        for digest in parse_tagged(response, "DIGEST"):
            await _publish_vitals(digest)

        for alert in parse_tagged(response, "ALERT"):
            await _publish_alert(alert)

        log.info("Claude escalation complete", extra={"component": component})

    except Exception:
        log.exception("Claude escalation failed", extra={"component": component})


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

        system_state = _build_system_state()
        recent_actions_str = json.dumps(_recent_actions[-10:], indent=2, default=str) if _recent_actions else "None"
        config = await load_kv_config(_config_kv, _default_config)
        config_str = json.dumps(config, indent=2)

        prompt = _system_prompt.format(
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
            response, _usage = await invoke_claude(
                prompt,
                model=MODEL,
                semaphore=_semaphore,
                max_turns=MAX_CLAUDE_TURNS,
                mcp_servers={"maki-immune": _mcp_server},
            )

            config_updates = parse_config_tags(response)
            await apply_config_updates(
                _config_kv, config_updates, allowed_keys=set(_default_config.keys()), validators=_config_validators
            )

            responses = parse_tagged(response, "RESPONSE")
            if responses:
                reply = "\n\n".join(responses)
            else:
                reply = response

            await _publish_immune_response(message_id, reply)

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

            system_state = _build_system_state()
            recent_actions_str = json.dumps(_recent_actions[-10:], indent=2) if _recent_actions else "None"
            config_str = json.dumps(config, indent=2)

            prompt = _system_prompt.format(
                system_state=system_state,
                recent_actions=recent_actions_str,
                config=config_str,
            )

            response, _usage = await invoke_claude(
                prompt,
                model=MODEL,
                semaphore=_semaphore,
                max_turns=MAX_CLAUDE_TURNS,
                mcp_servers={"maki-immune": _mcp_server},
            )

            config_updates = parse_config_tags(response)
            await apply_config_updates(
                _config_kv, config_updates, allowed_keys=set(_default_config.keys()), validators=_config_validators
            )

            if "[SILENT]" not in response:
                for digest in parse_tagged(response, "DIGEST"):
                    await _publish_vitals(digest)

            for alert in parse_tagged(response, "ALERT"):
                await _publish_alert(alert)

            log.info("Immune heartbeat complete", extra={"silent": "[SILENT]" in response})

        except Exception:
            log.exception("Immune heartbeat error")


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

        asyncio.create_task(escalate_to_claude("maki-cortex", state, reason))

    except Exception:
        log.exception("Cortex stuck handler error")
