"""Audit logger middleware — logs redactions for observability."""

from __future__ import annotations

import logging

from .base import Middleware, MiddlewareContext

log = logging.getLogger(__name__)


class AuditLogger(Middleware):
    """Logs all redactions and annotations from upstream middleware.

    This should be the last middleware in the pipeline so it can see
    everything that was scrubbed/annotated by earlier stages.
    Logs to structured logging (recall-visible), never to the LLM.
    """

    async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
        redactions = ctx.annotations.get("redactions")
        if redactions:
            total = sum(sum(entry["counts"].values()) for entry in redactions if "counts" in entry)
            log.info(
                "Middleware redactions applied",
                extra={
                    "total_redactions": total,
                    "details": redactions,
                    "model": ctx.model,
                    "mode": ctx.mode,
                },
            )

        size_info = ctx.annotations.get("size_guard")
        if size_info:
            log.debug("Context size", extra=size_info)

        return ctx
