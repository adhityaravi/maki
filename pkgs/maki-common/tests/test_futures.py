"""Tests for ``maki_common.futures.PendingFutures`` and ``PendingQueues``.

Two clusters:
  1. ``session()`` guarantees ``remove()`` on exit — including on exception
     or cancellation.
  2. The raw create/resolve/remove/push/cancel_all API contracts these
     primitives expose to stem/cortex/recall (issue #140).

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


# --- PendingFutures: raw create/resolve/remove/__contains__ ------------------


def test_pending_futures_create_then_resolve_delivers_value() -> None:
    """create() registers the key; resolve() returns True and awaiter gets value."""
    pending = PendingFutures()

    async def scenario() -> None:
        future = pending.create("k")
        assert "k" in pending
        assert pending.has("k") is True
        assert pending.resolve("k", 123) is True
        assert await future == 123

    _run(scenario())


def test_pending_futures_resolve_unknown_key_returns_false() -> None:
    pending = PendingFutures()

    async def scenario() -> None:
        assert pending.resolve("missing", 1) is False

    _run(scenario())


def test_pending_futures_resolve_already_done_returns_false() -> None:
    """Second resolve on an already-set future must not raise — just return False."""
    pending = PendingFutures()

    async def scenario() -> None:
        future = pending.create("k")
        assert pending.resolve("k", "first") is True
        # Future is now done; a second resolve is a no-op.
        assert pending.resolve("k", "second") is False
        assert await future == "first"

    _run(scenario())


def test_pending_futures_remove_missing_is_noop() -> None:
    """remove() must tolerate keys it doesn't know — used in timeout cleanup paths."""
    pending = PendingFutures()
    # No exception, no state change.
    pending.remove("never-registered")
    assert "never-registered" not in pending


def test_pending_futures_remove_clears_key() -> None:
    pending = PendingFutures()

    async def scenario() -> None:
        pending.create("k")
        assert "k" in pending
        pending.remove("k")
        assert "k" not in pending
        assert pending.has("k") is False

    _run(scenario())


def test_pending_futures_contains_and_has_agree() -> None:
    """`x in pending` and pending.has(x) are the same predicate."""
    pending = PendingFutures()

    async def scenario() -> None:
        pending.create("a")
        assert ("a" in pending) is pending.has("a") is True
        assert ("b" in pending) is pending.has("b") is False

    _run(scenario())


# --- PendingQueues: raw push/cancel_all/remove/pending_keys ------------------


def test_pending_queues_create_and_push_delivers_value() -> None:
    pending = PendingQueues()

    async def scenario() -> None:
        queue = pending.create("k")
        assert "k" in pending
        assert pending.has("k") is True
        assert pending.push("k", {"chunk": "hi"}) is True
        assert await queue.get() == {"chunk": "hi"}

    _run(scenario())


def test_pending_queues_push_unknown_key_returns_false() -> None:
    pending = PendingQueues()
    assert pending.push("nope", {"chunk": "x"}) is False


def test_pending_queues_remove_missing_is_noop() -> None:
    pending = PendingQueues()
    pending.remove("never-registered")
    assert "never-registered" not in pending


def test_pending_queues_pending_keys_lists_open_queues() -> None:
    pending = PendingQueues()

    async def scenario() -> None:
        pending.create("a")
        pending.create("b")
        keys = pending.pending_keys()
        # Order isn't part of the contract — compare as a set.
        assert set(keys) == {"a", "b"}
        pending.remove("a")
        assert pending.pending_keys() == ["b"]

    _run(scenario())


def test_pending_queues_cancel_all_delivers_sentinel_to_consumer() -> None:
    """cancel_all injects a done+cancelled sentinel so blocked awaiters wake up."""
    pending = PendingQueues()

    async def scenario() -> None:
        queue = pending.create("k")
        cancelled = pending.cancel_all()
        assert cancelled == 1
        chunk = await queue.get()
        assert chunk == {"response": "", "done": True, "cancelled": True}

    _run(scenario())


def test_pending_queues_cancel_all_counts_and_wakes_every_queue() -> None:
    """The returned count matches the number of live queues; each gets a sentinel."""
    pending = PendingQueues()

    async def scenario() -> None:
        q_a = pending.create("a")
        q_b = pending.create("b")
        q_c = pending.create("c")
        assert pending.cancel_all() == 3
        # Every queue receives exactly one sentinel.
        for q in (q_a, q_b, q_c):
            chunk = await q.get()
            assert chunk["done"] is True
            assert chunk["cancelled"] is True

    _run(scenario())


def test_pending_queues_cancel_all_on_empty_is_zero() -> None:
    pending = PendingQueues()
    assert pending.cancel_all() == 0


def test_pending_queues_cancel_keys_only_hits_requested_keys() -> None:
    """cancel_keys wakes only the named queues; the rest keep waiting."""
    pending = PendingQueues()

    async def scenario() -> None:
        q_a = pending.create("a")
        q_b = pending.create("b")
        q_c = pending.create("c")
        cancelled = pending.cancel_keys(["a", "c"])
        assert cancelled == 2
        # Named queues get the sentinel.
        chunk_a = await q_a.get()
        chunk_c = await q_c.get()
        assert chunk_a == {"response": "", "done": True, "cancelled": True}
        assert chunk_c == {"response": "", "done": True, "cancelled": True}
        # The unnamed queue is untouched — no sentinel to consume.
        assert q_b.empty()

    _run(scenario())


def test_pending_queues_cancel_keys_ignores_unknown_keys() -> None:
    """Unknown keys are silently skipped so callers can pass a stale snapshot."""
    pending = PendingQueues()

    async def scenario() -> None:
        q_a = pending.create("a")
        cancelled = pending.cancel_keys(["a", "does-not-exist"])
        assert cancelled == 1
        chunk = await q_a.get()
        assert chunk == {"response": "", "done": True, "cancelled": True}

    _run(scenario())


def test_pending_queues_cancel_keys_empty_list_is_zero() -> None:
    pending = PendingQueues()
    pending.create("a")
    assert pending.cancel_keys([]) == 0


def test_pending_queues_contains_and_has_agree() -> None:
    pending = PendingQueues()

    async def scenario() -> None:
        pending.create("a")
        assert ("a" in pending) is pending.has("a") is True
        assert ("b" in pending) is pending.has("b") is False

    _run(scenario())
