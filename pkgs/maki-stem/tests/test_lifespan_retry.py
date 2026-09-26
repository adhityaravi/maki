"""Tests for the ``_retry_startup`` helper used by stem's ``lifespan``.

These tests lock in the retry-on-transient-failure behaviour introduced in
#750 — before the fix, any single blip in ``asyncpg.create_pool``, an
``init_kv`` bucket create, or ``init_conversation_stream`` at pod-start
killed uvicorn outright and wedged the pod in K8s CrashLoopBackOff for
≥5 minutes, even after the upstream dependency recovered.

The tests drive ``asyncio.run`` directly (no pytest-asyncio dependency)
and swap ``asyncio.sleep`` for a no-op to keep the retry-backoff timings
out of the test suite's wall clock.
"""

from __future__ import annotations

import asyncio
from typing import Any

from maki_stem import main as stem_main


def _run(coro):
    return asyncio.run(coro)


class _SleepPatch:
    """Tiny context manager so tests don't need pytest fixtures.

    Replaces ``asyncio.sleep`` inside ``stem_main`` with a no-op recorder
    so the retry-backoff schedule can be asserted without waiting real
    seconds. Attribute-based swap via ``setattr`` keeps the type checker
    from complaining about assigning to the ``asyncio.sleep`` overload
    signature.
    """

    def __enter__(self) -> list[float]:
        self.recorded: list[float] = []

        async def _fake_sleep(seconds: float, *args: Any, **kwargs: Any) -> None:
            self.recorded.append(seconds)

        self.orig_sleep = stem_main.asyncio.sleep
        setattr(stem_main.asyncio, "sleep", _fake_sleep)
        return self.recorded

    def __exit__(self, *exc: Any) -> None:
        setattr(stem_main.asyncio, "sleep", self.orig_sleep)


def test_retry_startup_returns_immediately_on_success() -> None:
    """Happy path: the factory succeeds first try, no sleep, no retry."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    with _SleepPatch() as recorded:
        result = _run(stem_main._retry_startup("test", factory))

    assert result == "ok"
    assert calls == 1
    assert recorded == []  # no retry, no backoff sleep


def test_retry_startup_recovers_after_transient_failure() -> None:
    """A ConnectionRefusedError on attempt 1 retries and succeeds on attempt 2.

    This is the exact failure mode from #750 — pgvector briefly refuses the
    connection during a rolling restart. Under the old code (no retry) this
    tore the pod down; under ``_retry_startup`` it produces a warning log
    line and a working pool.
    """
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionRefusedError("[Errno 111] Connect call failed")
        return "pool"

    with _SleepPatch() as recorded:
        result = _run(stem_main._retry_startup("asyncpg.create_pool", factory))

    assert result == "pool"
    assert calls == 2
    # Exactly one backoff sleep (before attempt 2), at the base delay.
    assert recorded == [stem_main.STARTUP_RETRY_BASE_DELAY_S]


def test_retry_startup_backoff_doubles_and_caps() -> None:
    """Backoff schedule doubles per attempt and caps at ``STARTUP_RETRY_MAX_DELAY_S``."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls < 8:
            raise OSError("still down")
        return "up"

    with _SleepPatch() as recorded:
        result = _run(stem_main._retry_startup("test", factory))

    assert result == "up"
    assert calls == 8
    # 7 retries → 7 backoff sleeps, doubling from base until hitting the cap.
    base = stem_main.STARTUP_RETRY_BASE_DELAY_S
    cap = stem_main.STARTUP_RETRY_MAX_DELAY_S
    expected = []
    d = base
    for _ in range(7):
        expected.append(d)
        d = min(d * 2, cap)
    assert recorded == expected
    # The cap should have been reached within this window given the default
    # tunables — guard against a future edit that raises the cap silently.
    assert cap in recorded


def test_retry_startup_raises_after_max_attempts() -> None:
    """After ``max_attempts`` failures the final exception is re-raised.

    This is important: uvicorn's normal ``Application startup failed``
    path still fires so K8s reschedules the pod after roughly the same
    window CrashLoopBackOff would have imposed anyway — we just avoid
    the 5-minute penalty for a transient blip.
    """
    calls = 0
    sentinel = TimeoutError("dependency wedged")

    async def factory() -> str:
        nonlocal calls
        calls += 1
        raise sentinel

    raised: BaseException | None = None
    with _SleepPatch() as recorded:
        try:
            _run(stem_main._retry_startup("test", factory, max_attempts=3))
        except TimeoutError as exc:
            raised = exc

    assert raised is sentinel
    assert calls == 3
    # 3 attempts → 2 backoff sleeps between them; no sleep after the
    # final failure (we raise directly).
    assert len(recorded) == 2
