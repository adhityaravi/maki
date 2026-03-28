"""Async future management for request/response correlation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class PendingFutures:
    """Manage request/response correlation via asyncio futures.

    Usage:
        pending = PendingFutures()
        future = pending.create("msg-123")
        # ... later, when response arrives:
        pending.resolve("msg-123", response_data)
        # ... the awaiter gets the result:
        result = await future
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def create(self, key: str) -> asyncio.Future:
        """Create and register a future for the given key."""
        future = asyncio.get_event_loop().create_future()
        self._futures[key] = future
        return future

    def resolve(self, key: str, value: Any) -> bool:
        """Resolve a pending future. Returns True if found and resolved."""
        future = self._futures.get(key)
        if future and not future.done():
            future.set_result(value)
            return True
        return False

    def remove(self, key: str) -> None:
        """Remove a future (e.g. on timeout or cleanup)."""
        self._futures.pop(key, None)

    def has(self, key: str) -> bool:
        """Check if a future exists for the given key."""
        return key in self._futures

    def __contains__(self, key: str) -> bool:
        return key in self._futures
