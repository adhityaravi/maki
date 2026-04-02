"""K8s tools — investigation and remediation for maki-immune."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)


def make_k8s_tools(
    k8s_v1: Any,
    k8s_apps_v1: Any,
    namespace: str,
    nc: Any,
    acquire_lock: Any,
    release_lock: Any,
    restart_history: dict[str, list[float]],
    recent_actions: list[dict],
    config_getter: Any,
    deploy_history: dict[str, str] | None = None,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for K8s tools."""

    from maki_common.subjects import IMMUNE_ACTION

    if deploy_history is None:
        deploy_history = {}

    # --- Read-only tools ---

    async def list_pods(args: dict[str, Any]) -> dict[str, Any]:
        """List all pods in the maki namespace."""
        log.info("Tool: list_pods")
        try:
            pods = await asyncio.to_thread(k8s_v1.list_namespaced_pod, namespace=namespace)
            lines = []
            for pod in pods.items:
                name = pod.metadata.name
                phase = pod.status.phase
                ready = "Ready"
                restarts = 0
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if not cs.ready:
                            ready = "NotReady"
                        restarts += cs.restart_count

                age = ""
                if pod.metadata.creation_timestamp:
                    delta = datetime.now(UTC) - pod.metadata.creation_timestamp
                    hours = int(delta.total_seconds() / 3600)
                    minutes = int((delta.total_seconds() % 3600) / 60)
                    age = f"{hours}h{minutes}m"

                lines.append(f"  {name}  {phase}/{ready}  restarts={restarts}  age={age}")

            return mcp_result("Pods in namespace:\n" + "\n".join(lines))
        except Exception as e:
            return mcp_result(f"Failed to list pods: {e}")

    async def describe_pod(args: dict[str, Any]) -> dict[str, Any]:
        """Get detailed info about a specific pod."""
        pod_name = args.get("pod_name", "")
        log.info("Tool: describe_pod", extra={"pod_name": pod_name})
        try:
            pod = await asyncio.to_thread(k8s_v1.read_namespaced_pod, name=pod_name, namespace=namespace)
            lines = [f"Pod: {pod_name}"]
            lines.append(f"Phase: {pod.status.phase}")
            lines.append(f"Node: {pod.spec.node_name}")

            if pod.status.conditions:
                lines.append("Conditions:")
                for c in pod.status.conditions:
                    lines.append(f"  {c.type}: {c.status} (reason={c.reason})")

            if pod.status.container_statuses:
                lines.append("Containers:")
                for cs in pod.status.container_statuses:
                    lines.append(f"  {cs.name}:")
                    lines.append(f"    ready={cs.ready}, restarts={cs.restart_count}")
                    lines.append(f"    image={cs.image}")
                    if cs.state.running:
                        lines.append(f"    state=Running (since {cs.state.running.started_at})")
                    elif cs.state.waiting:
                        lines.append(
                            f"    state=Waiting (reason={cs.state.waiting.reason}, message={cs.state.waiting.message})"
                        )
                    elif cs.state.terminated:
                        t = cs.state.terminated
                        lines.append(
                            f"    state=Terminated (reason={t.reason}, exit_code={t.exit_code}, message={t.message})"
                        )
                    if cs.last_state and cs.last_state.terminated:
                        t = cs.last_state.terminated
                        lines.append(
                            f"    last_terminated: reason={t.reason}, exit_code={t.exit_code}, at={t.finished_at}"
                        )

            if pod.spec.containers:
                lines.append("Resource requests/limits:")
                for c in pod.spec.containers:
                    if c.resources:
                        req = c.resources.requests or {}
                        lim = c.resources.limits or {}
                        lines.append(f"  {c.name}: requests={dict(req)}, limits={dict(lim)}")

            return mcp_result("\n".join(lines))
        except Exception as e:
            return mcp_result(f"Failed to describe pod {pod_name}: {e}")

    async def get_pod_logs(args: dict[str, Any]) -> dict[str, Any]:
        """Read recent logs from a pod."""
        pod_name = args.get("pod_name", "")
        tail_lines = min(int(args.get("tail_lines", "100")), 500)
        log.info("Tool: get_pod_logs", extra={"pod_name": pod_name, "tail_lines": tail_lines})
        try:
            logs = await asyncio.to_thread(
                k8s_v1.read_namespaced_pod_log,
                name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines,
            )
            if not logs:
                return mcp_result(f"(empty logs for {pod_name})")
            if len(logs) > 4000:
                logs = logs[-4000:]
                logs = f"[truncated to last 4000 chars]\n{logs}"
            return mcp_result(logs)
        except Exception as e:
            return mcp_result(f"Failed to get logs for {pod_name}: {e}")

    async def get_k8s_events(args: dict[str, Any]) -> dict[str, Any]:
        """Read recent K8s events in the namespace."""
        involved_object = args.get("involved_object", "")
        log.info("Tool: get_k8s_events", extra={"involved_object": involved_object})
        try:
            if involved_object:
                field_selector = f"involvedObject.name={involved_object}"
                events = await asyncio.to_thread(
                    k8s_v1.list_namespaced_event,
                    namespace=namespace,
                    field_selector=field_selector,
                )
            else:
                events = await asyncio.to_thread(k8s_v1.list_namespaced_event, namespace=namespace)

            items = sorted(
                events.items,
                key=lambda e: e.last_timestamp or e.metadata.creation_timestamp or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )[:30]

            if not items:
                return mcp_result("No events found.")

            lines = []
            for e in items:
                ts = e.last_timestamp or e.metadata.creation_timestamp or "?"
                lines.append(f"  [{ts}] {e.reason}: {e.message} (object={e.involved_object.name}, count={e.count})")

            return mcp_result("Recent events:\n" + "\n".join(lines))
        except Exception as e:
            return mcp_result(f"Failed to get events: {e}")

    async def get_deployment_status(args: dict[str, Any]) -> dict[str, Any]:
        """Get deployment status including replicas and conditions."""
        deployment_name = args.get("deployment_name", "")
        log.info("Tool: get_deployment_status", extra={"deployment": deployment_name})
        try:
            dep = await asyncio.to_thread(
                k8s_apps_v1.read_namespaced_deployment,
                name=deployment_name,
                namespace=namespace,
            )
            lines = [f"Deployment: {deployment_name}"]
            lines.append(
                f"Replicas: desired={dep.spec.replicas}, "
                f"ready={dep.status.ready_replicas or 0}, "
                f"available={dep.status.available_replicas or 0}, "
                f"unavailable={dep.status.unavailable_replicas or 0}"
            )

            if dep.status.conditions:
                lines.append("Conditions:")
                for c in dep.status.conditions:
                    lines.append(f"  {c.type}: {c.status} (reason={c.reason})")

            if dep.spec.template.spec.containers:
                lines.append("Images:")
                for c in dep.spec.template.spec.containers:
                    lines.append(f"  {c.name}: {c.image}")

            return mcp_result("\n".join(lines))
        except Exception as e:
            return mcp_result(f"Failed to get deployment {deployment_name}: {e}")

    # --- Mutating tools ---

    async def _record_action(action: dict) -> None:
        """Record an action to history and publish to NATS."""
        recent_actions.append(action)
        if len(recent_actions) > 50:
            recent_actions.pop(0)
        try:
            await nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
        except Exception:
            log.exception("Failed to publish action")

    async def restart_pod(args: dict[str, Any]) -> dict[str, Any]:
        """Delete a pod so its deployment recreates it."""
        pod_name = args.get("pod_name", "")
        reason = args.get("reason", "no reason given")
        log.info("Tool: restart_pod", extra={"pod_name": pod_name, "reason": reason})

        config = await config_getter()
        max_restarts = config.get("reflex_restart_max", 3)
        now = time.time()
        hour_ago = now - 3600
        history = restart_history.get(pod_name, [])
        history = [t for t in history if t > hour_ago]
        restart_history[pod_name] = history

        if len(history) >= max_restarts:
            return mcp_result(
                f"DENIED: {pod_name} already restarted {len(history)} times "
                f"in the last hour (limit: {max_restarts}). "
                f"Try a different remediation approach."
            )

        if not await acquire_lock("immune-claude", ttl=60):
            return mcp_result("DENIED: infrastructure lock held by another process. Try again shortly.")

        try:
            await asyncio.to_thread(
                k8s_v1.delete_namespaced_pod,
                name=pod_name,
                namespace=namespace,
                grace_period_seconds=10,
            )
            history.append(now)
            restart_history[pod_name] = history

            await _record_action(
                {
                    "type": "claude_restart",
                    "pod_name": pod_name,
                    "reason": reason,
                    "timestamp": now,
                }
            )

            return mcp_result(f"Pod {pod_name} deleted (deployment will recreate it). Reason: {reason}")
        except Exception as e:
            return mcp_result(f"Failed to restart {pod_name}: {e}")
        finally:
            await release_lock("immune-claude")

    async def scale_deployment(args: dict[str, Any]) -> dict[str, Any]:
        """Scale a deployment to a specific number of replicas."""
        deployment_name = args.get("deployment_name", "")
        replicas = int(args.get("replicas", "1"))
        log.info(
            "Tool: scale_deployment",
            extra={"deployment": deployment_name, "replicas": replicas},
        )

        if replicas < 0 or replicas > 5:
            return mcp_result(f"DENIED: replicas must be between 0 and 5 (requested {replicas})")

        if not await acquire_lock("immune-claude", ttl=60):
            return mcp_result("DENIED: infrastructure lock held by another process.")

        try:
            body = {"spec": {"replicas": replicas}}
            await asyncio.to_thread(
                k8s_apps_v1.patch_namespaced_deployment_scale,
                name=deployment_name,
                namespace=namespace,
                body=body,
            )

            await _record_action(
                {
                    "type": "claude_scale",
                    "deployment": deployment_name,
                    "replicas": replicas,
                    "timestamp": time.time(),
                }
            )

            return mcp_result(f"Deployment {deployment_name} scaled to {replicas} replicas.")
        except Exception as e:
            return mcp_result(f"Failed to scale {deployment_name}: {e}")
        finally:
            await release_lock("immune-claude")

    async def restart_deployment(args: dict[str, Any]) -> dict[str, Any]:
        """Trigger a rolling restart of a deployment (recreates pods with same image)."""
        deployment_name = args.get("deployment_name", "")
        log.info("Tool: restart_deployment", extra={"deployment": deployment_name})

        if not await acquire_lock("immune-claude", ttl=60):
            return mcp_result("DENIED: infrastructure lock held by another process.")

        try:
            now = datetime.now(UTC).isoformat()
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now,
                            }
                        }
                    }
                }
            }
            await asyncio.to_thread(
                k8s_apps_v1.patch_namespaced_deployment,
                name=deployment_name,
                namespace=namespace,
                body=body,
            )

            await _record_action(
                {
                    "type": "claude_restart_deployment",
                    "deployment": deployment_name,
                    "timestamp": time.time(),
                }
            )

            return mcp_result(
                f"Rolling restart triggered for {deployment_name}. "
                f"K8s will gradually replace pods with fresh instances (same image)."
            )
        except Exception as e:
            return mcp_result(f"Failed to restart {deployment_name}: {e}")
        finally:
            await release_lock("immune-claude")

    async def rollback_deployment(args: dict[str, Any]) -> dict[str, Any]:
        """Rollback a deployment to its previous image version."""
        deployment_name = args.get("deployment_name", "")
        log.info("Tool: rollback_deployment", extra={"deployment": deployment_name})

        previous_image = deploy_history.get(deployment_name)
        if not previous_image:
            return mcp_result(
                f"No previous image recorded for {deployment_name}. "
                f"Cannot rollback — no deploy history available. "
                f"Use get_deployment_status to check current state."
            )

        if not await acquire_lock("immune-claude", ttl=60):
            return mcp_result("DENIED: infrastructure lock held by another process.")

        try:
            dep = await asyncio.to_thread(
                k8s_apps_v1.read_namespaced_deployment,
                name=deployment_name,
                namespace=namespace,
            )
            current_image = dep.spec.template.spec.containers[0].image
            container_name = dep.spec.template.spec.containers[0].name

            if current_image == previous_image:
                return mcp_result(f"{deployment_name} is already running {previous_image}. Nothing to rollback.")

            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": container_name,
                                    "image": previous_image,
                                }
                            ]
                        }
                    }
                }
            }
            await asyncio.to_thread(
                k8s_apps_v1.patch_namespaced_deployment,
                name=deployment_name,
                namespace=namespace,
                body=patch,
            )

            await _record_action(
                {
                    "type": "claude_rollback",
                    "deployment": deployment_name,
                    "from_image": current_image,
                    "to_image": previous_image,
                    "timestamp": time.time(),
                }
            )

            return mcp_result(
                f"Rolled back {deployment_name}: {current_image} → {previous_image}. "
                f"K8s will gradually replace pods with the previous version."
            )
        except Exception as e:
            return mcp_result(f"Failed to rollback {deployment_name}: {e}")
        finally:
            await release_lock("immune-claude")

    return [
        (
            "list_pods",
            "List all pods in the maki namespace with status, readiness, restarts, and age.",
            {},
            list_pods,
        ),
        (
            "describe_pod",
            "Get detailed info about a pod: conditions, container states, "
            "restart counts, last termination reason, resource limits.",
            {"pod_name": str},
            describe_pod,
        ),
        (
            "get_pod_logs",
            "Read recent logs from a pod. Returns up to 500 lines / 4000 chars.",
            {"pod_name": str, "tail_lines": str},
            get_pod_logs,
        ),
        (
            "get_k8s_events",
            "Read recent K8s events in the namespace. Optionally filter by involved object name.",
            {"involved_object": str},
            get_k8s_events,
        ),
        (
            "get_deployment_status",
            "Get deployment status: replicas, conditions, image versions.",
            {"deployment_name": str},
            get_deployment_status,
        ),
        (
            "restart_pod",
            "Delete a pod so its deployment recreates it. Rate-limited. Always provide a reason explaining why.",
            {"pod_name": str, "reason": str},
            restart_pod,
        ),
        (
            "scale_deployment",
            "Scale a deployment to a specific number of replicas (0-5).",
            {"deployment_name": str, "replicas": str},
            scale_deployment,
        ),
        (
            "restart_deployment",
            "Trigger a rolling restart of a deployment — recreates all pods with the same image. "
            "Use this for config changes or stuck pods, NOT for reverting bad deploys.",
            {"deployment_name": str},
            restart_deployment,
        ),
        (
            "rollback_deployment",
            "Rollback a deployment to its previous image version. Only works if a deploy was "
            "previously tracked. For rolling restarts (same image), use restart_deployment instead.",
            {"deployment_name": str},
            rollback_deployment,
        ),
    ]
