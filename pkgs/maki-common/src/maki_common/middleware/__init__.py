"""Pluggable middleware pipeline for LLM calls.

All middleware runs before LLM dispatch — zero LLM calls, sub-millisecond overhead.
"""

from .base import Middleware, MiddlewareContext, MiddlewareRejection
from .pipeline import MiddlewarePipeline, get_default_pipeline, register_middleware

__all__ = [
    "Middleware",
    "MiddlewareContext",
    "MiddlewarePipeline",
    "MiddlewareRejection",
    "get_default_pipeline",
    "register_middleware",
]
