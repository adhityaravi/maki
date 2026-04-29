"""Tests for ``maki_common.futures.PendingFutures`` and ``PendingQueues``.

Focus: the ``session()`` async context manager guarantees ``remove()`` is
called on exit — including when the body raises.

These tests drive an asyncio event loop directly with ``asyncio.run`` so the
project does not need pytest-asyncio.
"""

from __future__ import annotations

import asyncio

from maki_common.futures import PendingFutures, PendingQueues


def _run(coro):
    return asyncio.run(coro)


# --- PendingQueues.session ---------------------------------------------------


def test_pending_queues_session_removes_on_normal_exit() -> None:
    pending = PendingQueues()

    async def scenario() -> None:
        async with pending.session("k1") as queue:
            assert "k1" in pending
            assert isinstance(queue, asyncio.Queue)
        assert "k1" not in pending

    _run(scenario())


def test_pending_queues_session_removes_on_exception() -> None:
    pending = PendingQueues()

    async def scenario() -> None:
        raised = False
        try:
            async with pending.session("k2") as queue:
                assert "k2" in pending
                assert isinstance(queue, asyncio.Queue)
                raise RuntimeError("boom")
        except RuntimeError as exc:
            raised = True
            assert str(exc) == "boom"
        assert raised, "RuntimeError should propagate out of session()"
        # Still removed even though the body raised.
        assert "k2" not in pending

    _run(scenario())


def test_pending_queues_session_yields_working_queue() -> None:
    pending = PendingQueues()

    async def scenario() -> None:
        async with pending.session("k3") as queue:
            # External code can push via the manager…
            pending.push("k3", {"chunk": "hello", "done": False})
            pending.push("k3", {"chunk": "", "done": True})
            # …and the consumer reads from the yielded queue.
            first = await queue.get()
            second = await queue.get()
            assert first == {"chunk": "hello", "done": False}
            assert second == {"chunk": "", "done": True}

    _run(scenario())


def test_pending_queues_session_removes_on_cancel() -> None:
    """If the surrounding task is cancelled mid-await, the queue is still removed."""
    pending = PendingQueues()

    async def scenario() -> None:
        async def waiter() -> None:
            async with pending.session("k4") as queue:
                # Will block forever — caller will cancel us.
                await queue.get()

        task = asyncio.create_task(waiter())
        # Yield once so the task enters the session.
        await asyncio.sleep(0)
        assert "k4" in pending
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled, "task should have been cancelled"
        assert "k4" not in pending

    _run(scenario())


# --- PendingFutures.session --------------------------------------------------


def test_pending_futures_session_removes_on_normal_exit() -> None:
    pending = PendingFutures()

    async def scenario() -> None:
        async with pending.session("f1") as future:
            assert "f1" in pending
            assert isinstance(future, asyncio.Future)
        assert "f1" not in pending

    _run(scenario())


def test_pending_futures_session_removes_on_exception() -> None:
    pending = PendingFutures()

    async def scenario() -> None:
        raised = False
        try:
            async with pending.session("f2"):
                assert "f2" in pending
                raise ValueError("bad")
        except ValueError as exc:
            raised = True
            assert str(exc) == "bad"
        assert raised, "ValueError should propagate out of session()"
        assert "f2" not in pending

    _run(scenario())


def test_pending_futures_session_yields_resolvable_future() -> None:
    pending = PendingFutures()

    async def scenario() -> None:
        async with pending.session("f3") as future:
            # Resolve from "outside" via the manager.
            assert pending.resolve("f3", {"ok": True}) is True
            result = await future
            assert result == {"ok": True}

    _run(scenario())
