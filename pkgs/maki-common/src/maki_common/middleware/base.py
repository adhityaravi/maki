"""Abstract base class for middleware components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class MiddlewareRejection(Exception):
    """Raised by middleware to short-circuit the pipeline and reject the request."""

    def __init__(self, reason: str, middleware_name: str = "") -> None:
        self.reason = reason
        self.middleware_name = middleware_name
        super().__init__(f"Rejected by {middleware_name}: {reason}")


@dataclass
class MiddlewareContext:
    """Context passed through the middleware pipeline.

    Middleware can mutate prompt/system_prompt (scrub, redact) and
    add metadata via the annotations dict for audit/observability.
    """

    prompt: str
    system_prompt: str | None = None
    model: str = ""
    mode: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)


class Middleware(ABC):
    """Abstract middleware that processes LLM call context before dispatch."""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
        """Process the context and return it (possibly mutated).

        To reject/short-circuit, raise MiddlewareRejection.
        """
        ...
