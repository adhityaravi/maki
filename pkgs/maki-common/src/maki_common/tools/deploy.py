"""Deploy coordination tools — cortex requests, immune executes."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)


def make_deploy_tools(nc: Any) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for deploy tools."""
    from maki_common.subjects import DEPLOY_REQUEST, DEPLOY_STATUS_REQUEST, RESTART_REQUEST

    async def request_deploy(args: dict[str, Any]) -> dict[str, Any]:
        """Request deployment of a service via NATS (immune executes)."""
        service = args.get("service", "")
        image_tag = args.get("image_tag", "latest")
        log.info("Tool: request_deploy", extra={"service": service, "image_tag": image_tag})

        if not service:
            return mcp_result("Error: service name is required.")

        # Self-deploys (cortex/stem) kill the pod that issued the request,
        # so the NATS request/reply connection dies before immune can respond.
        # Fire-and-forget for these — immune publishes results to #maki-vitals.
        self_deploy = any(s in service for s in ("cortex", "stem"))

        try:
            payload = json.dumps(
                {
                    "service": service,
                    "image_tag": image_tag,
                    "requested_at": time.time(),
                }
            ).encode()

            if self_deploy:
                await nc.publish(DEPLOY_REQUEST, payload)
                return mcp_result(
                    f"Self-deploy of {service} requested (fire-and-forget). "
                    "This pod will restart during rollout. "
                    "Result will appear in #maki-vitals."
                )

            resp = await nc.request(DEPLOY_REQUEST, payload, timeout=120.0)
            return mcp_result(resp.data.decode())
        except Exception as e:
            return mcp_result(f"Deploy request failed: {e}")

    async def get_deploy_status(args: dict[str, Any]) -> dict[str, Any]:
        """Get current deployment status for a service."""
        service = args.get("service", "")
        log.info("Tool: get_deploy_status", extra={"service": service})

        if not service:
            return mcp_result("Error: service name is required.")

        try:
            payload = json.dumps({"service": service}).encode()
            resp = await nc.request(DEPLOY_STATUS_REQUEST, payload, timeout=10.0)
            return mcp_result(resp.data.decode())
        except Exception as e:
            return mcp_result(f"Status request failed: {e}")

    async def request_restart(args: dict[str, Any]) -> dict[str, Any]:
        """Request a rollout restart of a service via NATS (immune executes)."""
        service = args.get("service", "")
        reason = args.get("reason", "cortex request")
        log.info("Tool: request_restart", extra={"service": service, "reason": reason})

        if not service:
            return mcp_result("Error: service name is required.")

        # Restarting stem kills the init container pod, so the reply connection dies.
        # Fire-and-forget for stem — immune publishes results to #maki-vitals.
        self_restart = "stem" in service

        try:
            payload = json.dumps(
                {
                    "service": service,
                    "reason": reason,
                    "requested_at": time.time(),
                }
            ).encode()

            if self_restart:
                await nc.publish(RESTART_REQUEST, payload)
                return mcp_result(
                    f"Restart of {service} requested (fire-and-forget). "
                    "Init container will re-run and pull latest maki-loops. "
                    "Result will appear in #maki-vitals."
                )

            resp = await nc.request(RESTART_REQUEST, payload, timeout=120.0)
            return mcp_result(resp.data.decode())
        except Exception as e:
            return mcp_result(f"Restart request failed: {e}")

    return [
        (
            "request_deploy",
            "Request deployment of a Maki service. Sends the request to maki-immune which "
            "handles the actual K8s deployment, monitors health for 60 seconds, and auto-rollbacks "
            "if the new version is unhealthy. Returns the deploy result.",
            {"service": str, "image_tag": str},
            request_deploy,
        ),
        (
            "get_deploy_status",
            "Get the current deployment status of a Maki service — current image, pod status, and rollout state.",
            {"service": str},
            get_deploy_status,
        ),
        (
            "request_restart",
            "Request a rollout restart of a Maki service without changing its image. "
            "Immune patches the deployment annotation triggering a pod restart. "
            "For stem, this re-runs the init container which pulls the latest maki-loops code. "
            "Use this instead of request_deploy when only loop code has changed.",
            {"service": str, "reason": str},
            request_restart,
        ),
    ]
