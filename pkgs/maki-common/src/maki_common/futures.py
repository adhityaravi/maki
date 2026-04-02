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


class PendingQueues:
    """Manage streaming request/response correlation via asyncio queues.

    Like PendingFutures but for streaming — each push adds to the queue
    and the consumer reads chunks until a sentinel arrives.

    Usage:
        pending = PendingQueues()
        queue = pending.create("msg-123")
        # ... later, as chunks arrive:
        pending.push("msg-123", {"response": "chunk", "done": False})
        pending.push("msg-123", {"response": "", "done": True})
        # ... the consumer reads:
        while True:
            chunk = await queue.get()
            if chunk["done"]:
                break
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def create(self, key: str) -> asyncio.Queue:
        """Create and register a queue for the given key."""
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[key] = queue
        return queue

    def push(self, key: str, value: Any) -> bool:
        """Push a value to a pending queue. Returns True if found."""
        queue = self._queues.get(key)
        if queue is not None:
            queue.put_nowait(value)
            return True
        return False

    def remove(self, key: str) -> None:
        """Remove a queue."""
        self._queues.pop(key, None)

    def cancel_all(self) -> int:
        """Inject a done signal into all pending queues to unblock consumers.

        Returns the number of queues cancelled.
        """
        cancelled = 0
        for key in list(self._queues):
            queue = self._queues.get(key)
            if queue is not None:
                queue.put_nowait({"response": "", "done": True, "cancelled": True})
                cancelled += 1
        return cancelled

    def pending_keys(self) -> list[str]:
        """Return list of currently pending turn IDs."""
        return list(self._queues.keys())

    def has(self, key: str) -> bool:
        """Check if a queue exists for the given key."""
        return key in self._queues

    def __contains__(self, key: str) -> bool:
        return key in self._queues
