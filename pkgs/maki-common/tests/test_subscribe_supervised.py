"""Tests for ``maki_common.nats.subscribe_supervised``.

These exercise the supervisor's two key behaviours:

1. When the underlying ``async for sub.messages`` returns, the helper logs a
   warning and re-subscribes (instead of letting the task complete silently —
   the bug class issue #175 is about).
2. When ``nc.subscribe`` itself raises, the helper logs and retries with
   exponential backoff.

The tests fake a NATS client so they do not require a running server.
"""

from __future__ import annotations

import asyncio
import logging

from maki_common.nats import subscribe_supervised


class _FakeMessage:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSubscription:
    """Async-iterable that yields the supplied messages then exits."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.messages = self  # subscribe_supervised does ``async for msg in sub.messages``
        self._queue = list(messages)

    def __aiter__(self) -> _FakeSubscription:
        return self

    async def __anext__(self) -> _FakeMessage:
        if not self._queue:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._queue.pop(0)


class _FakeNC:
    """Minimal NATS client double — records subscribe calls."""

    def __init__(self, subs: list[_FakeSubscription | Exception]) -> None:
        self._subs = list(subs)
        self.subscribe_calls = 0
        self.is_connected = True

    async def subscribe(self, subject: str, queue: str | None = None) -> _FakeSubscription:
        self.subscribe_calls += 1
        if not self._subs:
            # Block forever after exhausting the script — caller will cancel us.
            await asyncio.Event().wait()
            raise RuntimeError("unreachable")
        item = self._subs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run(coro):
    return asyncio.run(coro)


def test_resubscribes_when_message_iterator_exits(caplog) -> None:  # type: ignore[no-untyped-def]
    """If ``async for sub.messages`` returns, the helper logs WARNING and re-subscribes."""

    async def scenario() -> tuple[int, list[bytes]]:
        seen: list[bytes] = []

        async def handler(msg: _FakeMessage) -> None:
            seen.append(msg.data)

        first = _FakeSubscription([_FakeMessage(b"first")])
        second = _FakeSubscription([_FakeMessage(b"second")])
        nc = _FakeNC([first, second])

        task = asyncio.create_task(subscribe_supervised(nc, "test.subject", handler, base_delay=0.01, max_delay=0.01))
        # Wait until both scripted subs have been consumed.
        for _ in range(200):
            if seen == [b"first", b"second"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return nc.subscribe_calls, seen

    with caplog.at_level(logging.WARNING, logger="maki_common.nats"):
        calls, seen = _run(scenario())

    assert seen == [b"first", b"second"]
    assert calls >= 2, f"Expected at least 2 subscribe calls, got {calls}"
    assert any("stream ended" in record.getMessage() for record in caplog.records), (
        f"Expected re-subscribe WARN, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_retries_when_subscribe_call_raises(caplog) -> None:  # type: ignore[no-untyped-def]
    """A failing ``nc.subscribe`` is logged and retried after backoff."""

    class _SubBoom(RuntimeError):
        pass

    async def scenario() -> tuple[int, list[bytes]]:
        seen: list[bytes] = []

        async def handler(msg: _FakeMessage) -> None:
            seen.append(msg.data)

        good = _FakeSubscription([_FakeMessage(b"after-retry")])
        nc = _FakeNC([_SubBoom("nope"), good])

        task = asyncio.create_task(subscribe_supervised(nc, "test.subject", handler, base_delay=0.01, max_delay=0.01))
        for _ in range(200):
            if seen == [b"after-retry"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return nc.subscribe_calls, seen

    with caplog.at_level(logging.ERROR, logger="maki_common.nats"):
        calls, seen = _run(scenario())

    assert seen == [b"after-retry"]
    assert calls >= 2
    assert any("Supervised subscribe failed" in record.getMessage() for record in caplog.records), (
        f"Expected ERROR log on subscribe failure, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_handler_exception_does_not_kill_loop(caplog) -> None:  # type: ignore[no-untyped-def]
    """A handler that raises is logged but the loop keeps consuming."""

    async def scenario() -> list[bytes]:
        seen: list[bytes] = []

        async def handler(msg: _FakeMessage) -> None:
            if msg.data == b"boom":
                raise RuntimeError("handler failed")
            seen.append(msg.data)

        sub = _FakeSubscription([_FakeMessage(b"ok-1"), _FakeMessage(b"boom"), _FakeMessage(b"ok-2")])
        nc = _FakeNC([sub])

        task = asyncio.create_task(subscribe_supervised(nc, "test.subject", handler, base_delay=0.01, max_delay=0.01))
        for _ in range(200):
            if seen == [b"ok-1", b"ok-2"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return seen

    with caplog.at_level(logging.ERROR, logger="maki_common.nats"):
        seen = _run(scenario())

    assert seen == [b"ok-1", b"ok-2"]
    assert any("Supervised handler error" in r.getMessage() for r in caplog.records)


def test_auto_ack_acks_messages_for_jetstream() -> None:
    """When ``auto_ack=True``, every dispatched message is acked even if the handler raises."""

    class _FakeJS:
        def __init__(self, sub: _FakeSubscription) -> None:
            self._sub = sub
            self.subscribe_calls = 0

        async def subscribe(self, subject: str, **kwargs: object) -> _FakeSubscription:
            self.subscribe_calls += 1
            if self._sub is None:
                await asyncio.Event().wait()
                raise RuntimeError("unreachable")
            sub = self._sub
            self._sub = None  # type: ignore[assignment]
            return sub

    async def scenario() -> tuple[bool, bool]:
        ok_msg = _FakeMessage(b"ok")
        bad_msg = _FakeMessage(b"bad")

        async def handler(msg: _FakeMessage) -> None:
            if msg.data == b"bad":
                raise RuntimeError("handler exploded")

        sub = _FakeSubscription([ok_msg, bad_msg])
        js = _FakeJS(sub)
        nc = _FakeNC([])

        task = asyncio.create_task(
            subscribe_supervised(
                nc,
                "test.subject",
                handler,
                js=js,
                durable="test-durable",
                base_delay=0.01,
                max_delay=0.01,
            )
        )
        for _ in range(200):
            if ok_msg.acked and bad_msg.acked:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ok_msg.acked, bad_msg.acked

    ok_acked, bad_acked = _run(scenario())
    assert ok_acked is True
    assert bad_acked is True
