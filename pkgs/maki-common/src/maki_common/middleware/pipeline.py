"""Ordered middleware pipeline with short-circuit support."""

from __future__ import annotations

import logging
import time

from .base import Middleware, MiddlewareContext, MiddlewareRejection

log = logging.getLogger(__name__)


class MiddlewarePipeline:
    """Runs an ordered chain of middleware before LLM dispatch.

    Each middleware receives the context, can mutate it, annotate it,
    or raise MiddlewareRejection to abort the call.
    """

    def __init__(self) -> None:
        self._chain: list[Middleware] = []

    def add(self, mw: Middleware) -> MiddlewarePipeline:
        """Append a middleware to the chain. Returns self for chaining."""
        self._chain.append(mw)
        return self

    @property
    def middlewares(self) -> list[Middleware]:
        return list(self._chain)

    async def run(self, ctx: MiddlewareContext) -> MiddlewareContext:
        """Execute all middleware in order.

        Returns the (possibly mutated) context.
        Raises MiddlewareRejection if any middleware short-circuits.
        """
        t0 = time.monotonic()
        for mw in self._chain:
            try:
                ctx = await mw.process(ctx)
            except MiddlewareRejection:
                log.warning(
                    "Middleware rejected request",
                    extra={"middleware": mw.name, "model": ctx.model, "mode": ctx.mode},
                )
                raise
        elapsed_ms = (time.monotonic() - t0) * 1000
        log.debug("Middleware pipeline complete", extra={"elapsed_ms": round(elapsed_ms, 2)})
        return ctx


# --- Global default pipeline ---

_default_pipeline: MiddlewarePipeline | None = None


def get_default_pipeline() -> MiddlewarePipeline:
    """Return the global default pipeline, creating it lazily with V1 middleware."""
    global _default_pipeline  # noqa: PLW0603
    if _default_pipeline is None:
        _default_pipeline = _build_default_pipeline()
    return _default_pipeline


def _build_default_pipeline() -> MiddlewarePipeline:
    from .audit import AuditLogger
    from .pii import PIIScrubber
    from .secrets import SecretDetector
    from .size_guard import SizeGuard

    pipeline = MiddlewarePipeline()
    pipeline.add(PIIScrubber())
    pipeline.add(SecretDetector())
    pipeline.add(SizeGuard())
    pipeline.add(AuditLogger())
    return pipeline


def register_middleware(mw: Middleware) -> None:
    """Add a middleware to the global default pipeline."""
    get_default_pipeline().add(mw)
