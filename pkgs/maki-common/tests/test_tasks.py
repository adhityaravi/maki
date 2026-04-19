"""Tests for ``maki_common.tasks.spawn_background``.

These tests drive an asyncio event loop directly with ``asyncio.run`` so the
project does not need pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import gc
import logging

from maki_common.tasks import (
    _BACKGROUND_TASKS,
    active_background_task_count,
    spawn_background,
)


def _run(coro):
    return asyncio.run(coro)


def test_spawn_background_runs_coro_to_completion() -> None:
    """The coroutine runs and the task is removed from the anchor set afterwards."""

    async def scenario() -> asyncio.Task:
        ran = asyncio.Event()

        async def body() -> None:
            ran.set()

        task = spawn_background(body())
        assert task in _BACKGROUND_TASKS
        await task
        # Let the done-callback (scheduled on the loop) fire.
        await asyncio.sleep(0)
        assert ran.is_set()
        return task

    task = _run(scenario())
    assert task not in _BACKGROUND_TASKS


def test_spawn_background_anchors_against_gc() -> None:
    """The task survives a GC pass even when the caller drops its reference."""

    async def scenario() -> None:
        started = asyncio.Event()
        finished = asyncio.Event()

        async def body() -> None:
            started.set()
            # Yield so the caller can run gc while we're alive.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            finished.set()

        # Discard the returned handle — the anchor set is the ONLY strong ref.
        spawn_background(body(), name="anchor-test")
        await started.wait()
        gc.collect()
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        await asyncio.sleep(0)

    _run(scenario())


def test_spawn_background_logs_exception(caplog) -> None:  # type: ignore[no-untyped-def]
    """An exception inside the coroutine is logged, not swallowed."""

    class BoomError(RuntimeError):
        pass

    async def scenario() -> asyncio.Task:
        async def body() -> None:
            raise BoomError("kaboom")

        task = spawn_background(body(), name="boom")
        # Drain the task (it will raise) and then let the done-callback fire.
        try:
            await task
        except BoomError:
            pass
        await asyncio.sleep(0)
        return task

    with caplog.at_level(logging.ERROR, logger="maki_common.tasks"):
        task = _run(scenario())

    assert task not in _BACKGROUND_TASKS
    assert any(
        "Background task" in record.getMessage() and "failed" in record.getMessage() for record in caplog.records
    ), f"Expected failure log, got: {[r.getMessage() for r in caplog.records]}"
    assert any(record.exc_info and isinstance(record.exc_info[1], BoomError) for record in caplog.records)


def test_spawn_background_ignores_cancelled_task(caplog) -> None:  # type: ignore[no-untyped-def]
    """A cancelled task is cleaned up silently — no error log."""

    async def scenario() -> asyncio.Task:
        async def body() -> None:
            await asyncio.sleep(60)

        task = spawn_background(body(), name="cancel-me")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return task

    with caplog.at_level(logging.ERROR, logger="maki_common.tasks"):
        task = _run(scenario())

    assert task not in _BACKGROUND_TASKS
    assert not any("failed" in record.getMessage() for record in caplog.records)


def test_active_background_task_count_tracks_lifecycle() -> None:
    """The count helper reflects the anchor-set size."""

    async def scenario() -> tuple[int, int, int]:
        before = active_background_task_count()

        async def body() -> None:
            await asyncio.sleep(0)

        task = spawn_background(body())
        during = active_background_task_count()
        await task
        await asyncio.sleep(0)
        after = active_background_task_count()
        return before, during, after

    before, during, after = _run(scenario())
    assert during == before + 1
    assert after == before


def test_spawn_background_sets_name() -> None:
    """The ``name`` kwarg is forwarded to the underlying task."""

    async def scenario() -> str:
        async def body() -> None:
            return None

        task = spawn_background(body(), name="my-task")
        await task
        await asyncio.sleep(0)
        return task.get_name()

    assert _run(scenario()) == "my-task"
