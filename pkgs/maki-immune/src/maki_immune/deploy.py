"""Deploy + restart coordination for maki-immune.

Handles deploy requests (canary → monitor → rollback → propagate),
restart requests (same canary/propagate pattern for init container re-runs),
and deploy status queries.

Replaces the pre-#102/#167 pattern of ``init()`` + module-level ``_nc: Any =
None`` sentinels with :class:`ImmuneDeployCoordinator` — every collaborator is
now injected via ``__init__`` and stored as an instance attribute, so IDEs
type-check the surface, the module can be safely touched before wiring is
complete, and each subsystem is testable in isolation.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

from maki_common import subscribe_supervised
from maki_common.subjects import (
    DEPLOY_PROPAGATE,
    IMMUNE_ACTION,
    RESTART_PROPAGATE,
)

from maki_immune.lock import LockNotAcquired

log = logging.getLogger(__name__)


def _normalize_image_tag(tag: str) -> str:
    """Normalize image tag to match Docker workflow convention.

    Pure function — no dependency on coordinator state, so it stays module-level
    (and picklable for tests) rather than living as a static method.
    """
    if tag == "latest":
        return tag
    if tag.startswith("sha-"):
        return tag
    if re.fullmatch(r"[0-9a-f]{7,40}", tag):
        return f"sha-{tag[:7]}"
    raise ValueError(f"Invalid image tag '{tag}': expected 'latest', 'sha-<hex>', or a hex commit SHA")


class ImmuneDeployCoordinator:
    """Coordinates deploy + restart requests for the local immune instance.

    Owns every collaborator that ``deploy.py`` used to reach through the
    ``_nc / _js / _k8s_v1`` module globals. ``main.py`` constructs a single
    instance during startup and passes the bound handlers to NATS subscribers.
    """

    def __init__(
        self,
        *,
        nc: Any,
        js: Any,
        k8s_v1: Any,
        k8s_apps_v1: Any,
        deploy_history_kv: Any,
        namespace: str,
        instance_id: str,
        ghcr_prefix: str,
        recent_actions: list,
        recent_actions_max: int,
        deploy_history: dict[str, str],
        failed_image_blacklist: set[str],
        infra_lock: Any,
        publish_alert: Any,
        publish_vitals: Any,
        schedule_persist_recent_actions: Any,
    ) -> None:
        self._nc = nc
        self._js = js
        self._k8s_v1 = k8s_v1
        self._k8s_apps_v1 = k8s_apps_v1
        self._deploy_history_kv = deploy_history_kv
        self._namespace = namespace
        self._instance_id = instance_id
        self._ghcr_prefix = ghcr_prefix
        self._recent_actions = recent_actions
        self._recent_actions_max = recent_actions_max
        self._deploy_history = deploy_history
        self._failed_image_blacklist = failed_image_blacklist
        self._infra_lock = infra_lock
        self._publish_alert = publish_alert
        self._publish_vitals = publish_vitals
        self._schedule_persist = schedule_persist_recent_actions

    # --- Deploy History Persistence ---

    async def load_deploy_history(self) -> None:
        """Load deploy history from KV on startup."""
        try:
            keys = await self._deploy_history_kv.keys()
            for key in keys:
                entry = await self._deploy_history_kv.get(key)
                self._deploy_history[key] = entry.value.decode()
            if self._deploy_history:
                log.info("Deploy history loaded", extra={"entries": len(self._deploy_history)})
        except Exception:
            log.info("No deploy history found in KV (first run)")

    async def _save_deploy_history(self, deployment_name: str, previous_image: str) -> None:
        """Persist previous image to KV for crash recovery."""
        self._deploy_history[deployment_name] = previous_image
        try:
            await self._deploy_history_kv.put(deployment_name, previous_image.encode())
        except Exception:
            log.warning("Failed to persist deploy history to KV", extra={"deployment": deployment_name})

    async def _fetch_rollback_logs(self, deployment_name: str) -> str:
        """Fetch last 30 log lines from the failing pod before rollback."""
        if not self._k8s_v1:
            return ""
        try:
            pods = await asyncio.to_thread(
                self._k8s_v1.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=f"app={deployment_name}",
            )
            target_pod = None
            for pod in pods.items:
                ready = all(cs.ready for cs in (pod.status.container_statuses or []))
                if not ready:
                    target_pod = pod.metadata.name
                    break
            if not target_pod and pods.items:
                target_pod = pods.items[-1].metadata.name
            if not target_pod:
                return ""
            logs = await asyncio.to_thread(
                self._k8s_v1.read_namespaced_pod_log,
                name=target_pod,
                namespace=self._namespace,
                tail_lines=30,
            )
            if not logs:
                return ""
            if len(logs) > 1500:
                logs = logs[-1500:]
            return f"\n```\n{logs.strip()}\n```"
        except Exception:
            log.debug("Could not fetch rollback logs", exc_info=True)
            return ""

    # --- Helpers ---

    async def _restart_dependent(self, deployment_name: str, reason: str) -> None:
        """Restart a dependent deployment (e.g. stem after cortex rollback)."""
        if not self._k8s_apps_v1:
            return
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"immune.maki/restartedAt": str(time.time())},
                    }
                }
            }
        }
        await asyncio.to_thread(
            self._k8s_apps_v1.patch_namespaced_deployment,
            name=deployment_name,
            namespace=self._namespace,
            body=patch,
        )
        log.info("Restarted dependent deployment", extra={"deployment": deployment_name, "reason": reason})

    async def _record_action(self, action: dict) -> None:
        """Append action to recent_actions, trim, publish IMMUNE_ACTION, and schedule persist.

        Single source of truth: local action history and the published action stream
        are guaranteed to see the same payload — no risk of drift between two literals.
        """
        self._recent_actions.append(action)
        if len(self._recent_actions) > self._recent_actions_max:
            self._recent_actions.pop(0)
        self._schedule_persist()
        try:
            await self._nc.publish(IMMUNE_ACTION, json.dumps(action).encode())
        except Exception:
            log.exception("Failed to publish IMMUNE_ACTION")

    async def _rollback_to(
        self,
        deployment_name: str,
        container_name: str,
        previous_image: str,
        image_tag: str,
        alert_message: str,
    ) -> None:
        """Roll back a deployment to ``previous_image``, blacklist the failing tag, alert.

        Shared between ``deploy_request_handler`` and ``_deploy_propagate_handler`` —
        the only real difference between their rollback paths was the alert wording,
        which is passed in. Rollback logs are appended to the alert automatically.
        """
        rollback_patch = {
            "spec": {"template": {"spec": {"containers": [{"name": container_name, "image": previous_image}]}}}
        }
        await asyncio.to_thread(
            self._k8s_apps_v1.patch_namespaced_deployment,
            name=deployment_name,
            namespace=self._namespace,
            body=rollback_patch,
        )
        if image_tag and image_tag != "latest":
            self._failed_image_blacklist.add(image_tag)
            log.warning(
                "Image tag added to blacklist",
                extra={"image_tag": image_tag, "blacklist_size": len(self._failed_image_blacklist)},
            )
        rollback_logs = await self._fetch_rollback_logs(deployment_name)
        await self._publish_alert(f"{alert_message}{rollback_logs}")

    # --- Rollout Monitoring ---

    async def _monitor_rollout(self, deployment_name: str, timeout: int = 60) -> bool:
        """Monitor deployment health after image update. Returns True if healthy."""
        deadline = time.time() + timeout
        await asyncio.sleep(5)

        while time.time() < deadline:
            try:
                dep = await asyncio.to_thread(
                    self._k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=self._namespace
                )
                status = dep.status
                desired = dep.spec.replicas or 1
                ready = status.ready_replicas or 0
                updated = status.updated_replicas or 0
                available = status.available_replicas or 0

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
                    log.warning("Rollout failed (Progressing=False)", extra={"deployment": deployment_name})
                    return False

                pods_healthy = True
                if self._k8s_v1:
                    pods = await asyncio.to_thread(
                        self._k8s_v1.list_namespaced_pod,
                        namespace=self._namespace,
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
                    if time.time() > deadline - 30:
                        log.warning(
                            "Pods stuck past grace period, failing rollout", extra={"deployment": deployment_name}
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

    # --- Deploy Request Handler ---

    async def deploy_request_handler(self, msg) -> None:
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
            image = f"{self._ghcr_prefix}/{deployment_name}:{image_tag}"

            log.info(
                "Deploy request received",
                extra={"service": service, "raw_tag": raw_tag, "normalized_tag": image_tag, "image": image},
            )

            # Blacklist check
            force = request.get("force", False)
            if image_tag != "latest" and not force and image_tag in self._failed_image_blacklist:
                log.warning(
                    "Deploy rejected: image tag is blacklisted",
                    extra={"image_tag": image_tag, "deployment": deployment_name},
                )
                result = {
                    "status": "blacklisted",
                    "message": (
                        f"Image {image_tag} is blacklisted — it failed a health check in this session. "
                        "Use force=true to override."
                    ),
                }
                await self._record_action(
                    {
                        "type": "deploy_rejected",
                        "deployment": deployment_name,
                        "image_tag": image_tag,
                        "reason": "blacklisted",
                        "timestamp": time.time(),
                    }
                )
                await msg.respond(json.dumps(result).encode())
                return

            if not self._k8s_apps_v1:
                await msg.respond(json.dumps({"status": "error", "message": "K8s client not available"}).encode())
                return

            try:
                async with self._infra_lock("immune-deploy", ttl=180):
                    dep = await asyncio.to_thread(
                        self._k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=self._namespace
                    )
                    previous_image = dep.spec.template.spec.containers[0].image
                    log.info("Current image recorded for rollback", extra={"previous": previous_image})

                    if previous_image == image:
                        log.info(
                            "Already on target image, skipping deploy",
                            extra={"deployment": deployment_name, "image": image},
                        )
                        result = {"status": "skipped", "message": f"Already running {image}"}
                        await msg.respond(json.dumps(result).encode())
                        return

                    await self._save_deploy_history(deployment_name, previous_image)

                    patch = {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [{"name": dep.spec.template.spec.containers[0].name, "image": image}]
                                }
                            }
                        }
                    }
                    await asyncio.to_thread(
                        self._k8s_apps_v1.patch_namespaced_deployment,
                        name=deployment_name,
                        namespace=self._namespace,
                        body=patch,
                    )
                    log.info("Deployment patched", extra={"deployment": deployment_name, "image": image})

                    healthy = await self._monitor_rollout(deployment_name, timeout=90)

                    if healthy:
                        result = {
                            "status": "success",
                            "message": f"Deployed {deployment_name} with {image}",
                            "image": image,
                        }
                        log.info("Deploy succeeded", extra={"deployment": deployment_name})
                        await self._publish_vitals(
                            f"Deployed {deployment_name} → {image_tag} — healthy on {self._instance_id}"
                        )

                        await self._js.publish(
                            DEPLOY_PROPAGATE,
                            json.dumps(
                                {
                                    "service": service,
                                    "image_tag": image_tag,
                                    "deployment_name": deployment_name,
                                    "image": image,
                                    "canary_instance": self._instance_id,
                                }
                            ).encode(),
                        )
                    else:
                        log.warning("Deploy unhealthy, rolling back", extra={"deployment": deployment_name})
                        await self._rollback_to(
                            deployment_name=deployment_name,
                            container_name=dep.spec.template.spec.containers[0].name,
                            previous_image=previous_image,
                            image_tag=image_tag,
                            alert_message=(
                                f"Deploy of {deployment_name} → {image_tag} FAILED health check. "
                                f"Rolled back to {previous_image}"
                            ),
                        )
                        result = {
                            "status": "rolled_back",
                            "message": f"Deploy of {image} failed health check, rolled back to {previous_image}",
                        }

                        if "cortex" in deployment_name:
                            try:
                                await self._restart_dependent("maki-stem", reason="cortex rollback")
                            except Exception:
                                log.exception("Failed to restart stem after cortex rollback")

                    await self._record_action(
                        {
                            "type": "deploy",
                            "deployment": deployment_name,
                            "image": image,
                            "result": result["status"],
                            "timestamp": time.time(),
                        }
                    )
                    await msg.respond(json.dumps(result).encode())

            except LockNotAcquired:
                await msg.respond(
                    json.dumps({"status": "error", "message": "Infrastructure lock held, try again later"}).encode()
                )
                return

        except Exception:
            log.exception("Deploy request handler error")
            try:
                await msg.respond(json.dumps({"status": "error", "message": "Internal error"}).encode())
            except Exception:
                pass

    # --- Deploy Propagation ---

    async def deploy_propagate_listener(self) -> None:
        """JetStream durable consumer for deploy propagation.

        Wrapped in ``subscribe_supervised`` so a JS reconnect or stream drain
        doesn't silently terminate cross-site propagation (issue #175).
        """
        from nats.js.api import DeliverPolicy

        await subscribe_supervised(
            self._nc,
            DEPLOY_PROPAGATE,
            self._deploy_propagate_handler,
            js=self._js,
            durable=f"immune-propagate-{self._instance_id}",
            deliver_policy=DeliverPolicy.NEW,
            auto_ack=True,
            # Space redeliveries so we don't hammer JS while a local deploy
            # in flight (~90-150s under _monitor_rollout) is still holding
            # the infra lock. 60s means ≤3 retries cover the full lock TTL.
            # See issue #385 — without redelivery, propagation ACK-and-dropped
            # silently on lock contention and hive sites diverged.
            nak_delay=60.0,
            # Deploy propagation runs kubectl set image + monitor_rollout,
            # which can take ~90-150s in the normal path. Ten minutes is a
            # generous outer bound: if we've held the handler for ten
            # minutes something is truly wedged (stuck rollout, k8s API
            # black hole) and JS should get to redeliver rather than let
            # this pod's listener freeze indefinitely (#492).
            handler_timeout=600.0,
            name="deploy_propagate",
        )

    async def _deploy_propagate_handler(self, msg) -> None:
        """Handle deploy propagation from canary — deploy same image locally."""
        try:
            request = json.loads(msg.data.decode())
            canary = request.get("canary_instance", "")
            deployment_name = request.get("deployment_name", "")
            image = request.get("image", "")
            image_tag = request.get("image_tag", "")

            if canary == self._instance_id:
                return

            if image_tag and image_tag != "latest" and image_tag in self._failed_image_blacklist:
                log.warning(
                    "Propagated deploy skipped: blacklisted",
                    extra={"image_tag": image_tag, "deployment": deployment_name, "canary": canary},
                )
                await self._record_action(
                    {
                        "type": "deploy_propagation_skipped",
                        "deployment": deployment_name,
                        "image_tag": image_tag,
                        "reason": "blacklisted",
                        "instance": self._instance_id,
                        "timestamp": time.time(),
                    }
                )
                return

            log.info(
                "Deploy propagation received from canary",
                extra={"canary": canary, "deployment": deployment_name, "image": image},
            )

            if not self._k8s_apps_v1:
                log.error("K8s client not available, cannot propagate deploy")
                return

            try:
                async with self._infra_lock("immune-deploy", ttl=180):
                    dep = await asyncio.to_thread(
                        self._k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=self._namespace
                    )
                    previous_image = dep.spec.template.spec.containers[0].image

                    if previous_image == image:
                        log.info("Already on target image, skipping", extra={"deployment": deployment_name})
                        return

                    await self._save_deploy_history(deployment_name, previous_image)

                    patch = {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [{"name": dep.spec.template.spec.containers[0].name, "image": image}]
                                }
                            }
                        }
                    }
                    await asyncio.to_thread(
                        self._k8s_apps_v1.patch_namespaced_deployment,
                        name=deployment_name,
                        namespace=self._namespace,
                        body=patch,
                    )
                    log.info("Propagated deploy applied", extra={"deployment": deployment_name, "image": image})

                    healthy = await self._monitor_rollout(deployment_name, timeout=150)

                    if healthy:
                        result_status = "success"
                        log.info("Propagated deploy healthy", extra={"deployment": deployment_name})
                        await self._publish_vitals(
                            f"Propagated deploy of {deployment_name} → {image_tag} — healthy on {self._instance_id}"
                        )
                    else:
                        result_status = "rolled_back"
                        log.warning("Propagated deploy unhealthy, rolling back", extra={"deployment": deployment_name})
                        await self._rollback_to(
                            deployment_name=deployment_name,
                            container_name=dep.spec.template.spec.containers[0].name,
                            previous_image=previous_image,
                            image_tag=image_tag,
                            alert_message=(
                                f"Propagated deploy of {deployment_name} → {image_tag} FAILED on {self._instance_id}. "
                                f"Rolled back to {previous_image}. Canary ({canary}) was healthy — investigate."
                            ),
                        )

                    await self._record_action(
                        {
                            "type": "deploy_propagation",
                            "deployment": deployment_name,
                            "image": image,
                            "result": result_status,
                            "canary_instance": canary,
                            "instance": self._instance_id,
                            "timestamp": time.time(),
                        }
                    )
            except LockNotAcquired:
                # When the lock is held, first check if the target image is already
                # deployed — no propagation needed and no retry warranted. Only if
                # a real change is pending do we defer via NAK/redelivery.
                try:
                    dep = await asyncio.to_thread(
                        self._k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=self._namespace
                    )
                    if dep.spec.template.spec.containers[0].image == image:
                        log.info(
                            "Lock held but already on target image, skipping", extra={"deployment": deployment_name}
                        )
                        return
                except Exception:
                    pass
                # Re-raise so subscribe_supervised NAKs the message and JetStream
                # redelivers after nak_delay. Without this, auto_ack silently
                # ACKs the propagation and this site diverges from the canary
                # (issue #385). The "already on target image" early-out above
                # keeps redelivery idempotent on the eventual retry.
                log.warning(
                    "Deploy propagation deferred — infra lock held, will retry",
                    extra={"deployment": deployment_name, "image": image, "canary": canary},
                )
                raise

        except LockNotAcquired:
            # Bubble up to subscribe_supervised so JetStream redelivers.
            raise
        except Exception:
            log.exception("Deploy propagation error")

    # --- Deploy Status ---

    async def deploy_status_handler(self, msg) -> None:
        """Handle deploy status requests — return current image and pod state."""
        try:
            request = json.loads(msg.data.decode())
            service = request.get("service", "")
            deployment_name = f"maki-{service}" if not service.startswith("maki-") else service

            if not self._k8s_apps_v1:
                await msg.respond(json.dumps({"error": "K8s client not available"}).encode())
                return

            dep = await asyncio.to_thread(
                self._k8s_apps_v1.read_namespaced_deployment, name=deployment_name, namespace=self._namespace
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

    # --- Restart Request Handler ---

    async def restart_request_handler(self, msg) -> None:
        """Handle restart requests from cortex — trigger rollout restart without changing image.

        Used when init containers need to re-run (e.g., maki-stem pulling latest maki-loops)
        or when pods need fresh state without a full deploy.

        Follows the same canary/propagate pattern as deploy.
        """
        try:
            request = json.loads(msg.data.decode())
            service = request.get("service", "")
            reason = request.get("reason", "cortex request")
            deployment_name = f"maki-{service}" if not service.startswith("maki-") else service

            log.info(
                "Restart request received",
                extra={"service": service, "deployment": deployment_name, "reason": reason},
            )

            if not self._k8s_apps_v1:
                await msg.respond(json.dumps({"status": "error", "message": "K8s client not available"}).encode())
                return

            try:
                async with self._infra_lock("immune-restart", ttl=180):
                    patch = {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {
                                        "immune.maki/restartedAt": str(time.time()),
                                        "immune.maki/restartReason": reason,
                                    }
                                }
                            }
                        }
                    }
                    await asyncio.to_thread(
                        self._k8s_apps_v1.patch_namespaced_deployment,
                        name=deployment_name,
                        namespace=self._namespace,
                        body=patch,
                    )
                    log.info("Rollout restart triggered", extra={"deployment": deployment_name})

                    healthy = await self._monitor_rollout(deployment_name, timeout=90)

                    if healthy:
                        result = {"status": "success", "message": f"Restarted {deployment_name} successfully"}
                        log.info("Restart succeeded", extra={"deployment": deployment_name})
                        await self._publish_vitals(f"Restarted {deployment_name} — healthy on {self._instance_id}")

                        await self._js.publish(
                            RESTART_PROPAGATE,
                            json.dumps(
                                {
                                    "service": service,
                                    "deployment_name": deployment_name,
                                    "reason": reason,
                                    "canary_instance": self._instance_id,
                                }
                            ).encode(),
                        )
                    else:
                        result = {
                            "status": "unhealthy",
                            "message": f"Restart of {deployment_name} failed health check — not propagating",
                        }
                        log.warning("Restart unhealthy, not propagating", extra={"deployment": deployment_name})
                        await self._publish_alert(
                            f"Restart of {deployment_name} FAILED health check on {self._instance_id} — not propagating"
                        )

                    await self._record_action(
                        {
                            "type": "restart",
                            "deployment": deployment_name,
                            "reason": reason,
                            "result": result["status"],
                            "timestamp": time.time(),
                        }
                    )
                    await msg.respond(json.dumps(result).encode())

            except LockNotAcquired:
                await msg.respond(
                    json.dumps({"status": "error", "message": "Infrastructure lock held, try again later"}).encode()
                )
                return

        except Exception:
            log.exception("Restart request handler error")
            try:
                await msg.respond(json.dumps({"status": "error", "message": "Internal error"}).encode())
            except Exception:
                pass

    # --- Restart Propagation ---

    async def restart_propagate_listener(self) -> None:
        """JetStream durable consumer for restart propagation.

        Wrapped in ``subscribe_supervised`` so a JS reconnect or stream drain
        doesn't silently terminate cross-site propagation (issue #175).
        """
        from nats.js.api import DeliverPolicy

        await subscribe_supervised(
            self._nc,
            RESTART_PROPAGATE,
            self._restart_propagate_handler,
            js=self._js,
            durable=f"immune-restart-{self._instance_id}",
            deliver_policy=DeliverPolicy.NEW,
            auto_ack=True,
            # See _deploy_propagate_handler for the rationale — issue #385.
            nak_delay=60.0,
            # kubectl rollout restart + monitor typically completes in a
            # few minutes. Ten-minute outer bound catches a wedged rollout
            # or a stuck k8s API rather than let the listener freeze
            # forever on this pod (#492).
            handler_timeout=600.0,
            name="restart_propagate",
        )

    async def _restart_propagate_handler(self, msg) -> None:
        """Handle restart propagation from canary — restart same deployment locally."""
        try:
            request = json.loads(msg.data.decode())
            canary = request.get("canary_instance", "")
            deployment_name = request.get("deployment_name", "")
            reason = request.get("reason", "propagated restart")

            if canary == self._instance_id:
                return

            log.info(
                "Restart propagation received from canary",
                extra={"canary": canary, "deployment": deployment_name, "reason": reason},
            )

            if not self._k8s_apps_v1:
                log.error("K8s client not available, cannot propagate restart")
                return

            try:
                async with self._infra_lock("immune-restart", ttl=180):
                    patch = {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {
                                        "immune.maki/restartedAt": str(time.time()),
                                        "immune.maki/restartReason": f"propagated: {reason}",
                                    }
                                }
                            }
                        }
                    }
                    await asyncio.to_thread(
                        self._k8s_apps_v1.patch_namespaced_deployment,
                        name=deployment_name,
                        namespace=self._namespace,
                        body=patch,
                    )
                    log.info("Propagated restart applied", extra={"deployment": deployment_name})

                    healthy = await self._monitor_rollout(deployment_name, timeout=90)

                    if healthy:
                        result_status = "success"
                        log.info("Propagated restart healthy", extra={"deployment": deployment_name})
                        await self._publish_vitals(
                            f"Propagated restart of {deployment_name} — healthy on {self._instance_id}"
                        )
                    else:
                        result_status = "unhealthy"
                        log.warning("Propagated restart unhealthy", extra={"deployment": deployment_name})
                        await self._publish_alert(
                            f"Propagated restart of {deployment_name} FAILED health check on {self._instance_id}. "
                            f"Canary ({canary}) was healthy — investigate."
                        )

                    await self._record_action(
                        {
                            "type": "restart_propagation",
                            "deployment": deployment_name,
                            "reason": reason,
                            "result": result_status,
                            "canary_instance": canary,
                            "instance": self._instance_id,
                            "timestamp": time.time(),
                        }
                    )
            except LockNotAcquired:
                # Re-raise so subscribe_supervised NAKs and JetStream redelivers
                # after nak_delay. Without this, auto_ack silently ACKs the
                # propagation and the restart never lands here (issue #385).
                # Unlike deploy, there's no idempotent early-out — restart is
                # always an annotation bump, so every redelivery bumps again.
                # That's acceptable: at worst we do a redundant rollout restart
                # once the lock frees, which is safe.
                log.warning(
                    "Restart propagation deferred — infra lock held, will retry",
                    extra={"deployment": deployment_name, "reason": reason, "canary": canary},
                )
                raise

        except LockNotAcquired:
            # Bubble up to subscribe_supervised so JetStream redelivers.
            raise
        except Exception:
            log.exception("Restart propagation error")
