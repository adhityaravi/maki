"""Background task helpers.

Python's asyncio event loop holds only a **weak reference** to tasks created
via ``asyncio.create_task``. If the caller does not retain the returned handle,
the task may be garbage-collected mid-execution and its exception swallowed.

``spawn_background`` anchors fire-and-forget tasks in a module-level strong
reference set and logs any exception they raise, so a silently dropped coroutine
and a silently swallowed traceback both become visible.

See: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)

# Module-level strong-reference set. Python only keeps weak refs to tasks; storing
# them here prevents mid-flight garbage collection (see asyncio.create_task docs).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _on_done(task: asyncio.Task[Any]) -> None:
    """Remove the task from the anchor set and log any exception it raised."""
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.exception(
            "Background task %r failed",
            task.get_name(),
            exc_info=exc,
        )


def spawn_background(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Schedule ``coro`` as a fire-and-forget background task.

    Unlike a bare ``asyncio.create_task``:
    - The task is anchored in a module-level set so the GC cannot collect it
      before completion.
    - A done-callback logs any uncaught exception via ``log.exception`` instead
      of letting it disappear silently.
    - The task is removed from the anchor set once it finalises.

    Use this whenever the caller does NOT retain the returned task handle.
    If you capture the handle and ``await``/``cancel`` it yourself, a bare
    ``asyncio.create_task`` is still fine — the caller's reference keeps it alive.

    Args:
        coro: The coroutine to run in the background.
        name: Optional task name, surfaced in logs and ``Task.get_name()``.

    Returns:
        The scheduled ``asyncio.Task``. Callers may ignore it safely.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def active_background_task_count() -> int:
    """Return the number of currently anchored background tasks (for tests/debug)."""
    return len(_BACKGROUND_TASKS)
