"""Tests for the unified cortex-turn submission helpers in ``maki_stem.turn``.

These tests focus on the invariants that motivated issue #125:

- Every timeout — from every caller — publishes ``CORTEX_STUCK`` so immune
  can rescue a wedged cortex.
- Publish failures surface as :class:`TurnPublishError` so the work-loop
  can distinguish "NATS never delivered the request" (infra) from
  "cortex received but did not respond" (per-issue failure) — see #284.
- The pending-queue lifecycle is always cleaned up, even when the body
  raises.

The tests drive ``asyncio.run`` directly (no pytest-asyncio dependency)
and stub NATS + the surrounding ``StemContext`` with the minimum surface
the helpers actually touch.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from maki_common.futures import PendingQueues
from maki_common.subjects import CORTEX_STUCK, CORTEX_TURN_REQUEST
from maki_stem.turn import (
    TurnPublishError,
    new_turn_id,
    submit_turn_single,
    submit_turn_streaming,
)

# --- Stubs -------------------------------------------------------------------


class _StubNATS:
    """Records every publish call and can be configured to fail."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.published: list[tuple[str, dict]] = []
        self._fail_on = fail_on or set()

    async def publish(self, subject: str, data: bytes) -> None:
        if subject in self._fail_on:
            raise RuntimeError(f"NATS refused publish to {subject}")
        self.published.append((subject, json.loads(data.decode())))


@dataclass
class _StubCtx:
    """Minimum ``StemContext``-shaped stub for the helper's touch points."""

    nc: _StubNATS
    pending: PendingQueues = field(default_factory=PendingQueues)


def _run(coro):
    return asyncio.run(coro)


# --- new_turn_id -------------------------------------------------------------


def test_new_turn_id_uses_prefix_and_hex_suffix() -> None:
    tid = new_turn_id("idle")
    prefix, _, suffix = tid.partition("-")
    assert prefix == "idle"
    # 8 hex chars from uuid4().hex[:8]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_new_turn_id_is_unique() -> None:
    ids = {new_turn_id("x") for _ in range(50)}
    assert len(ids) == 50


# --- submit_turn_single ------------------------------------------------------


def test_submit_turn_single_publishes_request_and_returns_response() -> None:
    ctx = _StubCtx(_StubNATS())
    turn_id = "test-1"

    async def scenario() -> Any:
        async def responder() -> None:
            # Give submit_turn_single a chance to create the queue + publish.
            await asyncio.sleep(0)
            ctx.pending.push(turn_id, {"response": "hi", "done": True})

        task = asyncio.create_task(responder())
        result = await submit_turn_single(
            ctx,
            turn_id=turn_id,
            payload={"turn_id": turn_id, "prompt": "hello"},
            timeout=5,
            mode="idle",
        )
        await task
        return result

    result = _run(scenario())
    assert result == {"response": "hi", "done": True}
    subjects = [s for s, _ in ctx.nc.published]
    assert subjects == [CORTEX_TURN_REQUEST]
    assert ctx.nc.published[0][1]["turn_id"] == turn_id
    # Queue should be cleaned up.
    assert not ctx.pending.has(turn_id)


def test_submit_turn_single_publishes_cortex_stuck_on_timeout() -> None:
    """The core #125 fix: every caller — idle included — signals immune on timeout."""

    ctx = _StubCtx(_StubNATS())
    turn_id = "test-timeout"

    async def scenario() -> bool:
        raised = False
        try:
            await submit_turn_single(
                ctx,
                turn_id=turn_id,
                payload={"turn_id": turn_id},
                timeout=0.01,
                mode="idle",
                user_waiting=False,
            )
        except TimeoutError:
            raised = True
        return raised

    assert _run(scenario()), "submit_turn_single should re-raise TimeoutError"

    subjects = [s for s, _ in ctx.nc.published]
    assert CORTEX_TURN_REQUEST in subjects
    assert CORTEX_STUCK in subjects

    stuck_payload = next(p for s, p in ctx.nc.published if s == CORTEX_STUCK)
    assert stuck_payload == {
        "turn_id": turn_id,
        "mode": "idle",
        "timeout_seconds": 0.01,
        "user_waiting": False,
    }
    # Queue cleaned up even after the TimeoutError propagates.
    assert not ctx.pending.has(turn_id)


def test_submit_turn_single_raises_turn_publish_error_when_nats_fails() -> None:
    ctx = _StubCtx(_StubNATS(fail_on={CORTEX_TURN_REQUEST}))
    turn_id = "test-publish-fail"

    async def scenario() -> Exception | None:
        try:
            await submit_turn_single(
                ctx,
                turn_id=turn_id,
                payload={"turn_id": turn_id},
                timeout=1,
                mode="work",
            )
        except TurnPublishError as exc:
            return exc
        return None

    caught = _run(scenario())
    assert isinstance(caught, TurnPublishError)
    # Original NATS exception is preserved via __cause__ so callers can
    # inspect it if they want to log the underlying reason.
    assert isinstance(caught.__cause__, RuntimeError)

    # No CORTEX_STUCK — the turn never started, so signalling immune would
    # be misleading. Pending queue is still cleaned up.
    assert all(s != CORTEX_STUCK for s, _ in ctx.nc.published)
    assert not ctx.pending.has(turn_id)


def test_submit_turn_single_forwards_stuck_metadata_from_caller() -> None:
    ctx = _StubCtx(_StubNATS())

    async def scenario() -> None:
        try:
            await submit_turn_single(
                ctx,
                turn_id="tid",
                payload={"turn_id": "tid"},
                timeout=0.01,
                mode="work",
                user_waiting=False,
            )
        except TimeoutError:
            pass

    _run(scenario())
    stuck_payload = next(p for s, p in ctx.nc.published if s == CORTEX_STUCK)
    assert stuck_payload["mode"] == "work"
    assert stuck_payload["user_waiting"] is False


# --- submit_turn_streaming ---------------------------------------------------


def test_submit_turn_streaming_yields_all_chunks_until_done() -> None:
    ctx = _StubCtx(_StubNATS())
    turn_id = "stream-1"

    async def scenario() -> list[dict]:
        async def responder() -> None:
            await asyncio.sleep(0)
            ctx.pending.push(turn_id, {"response": "hel", "done": False})
            ctx.pending.push(turn_id, {"response": "lo", "done": False})
            ctx.pending.push(turn_id, {"response": "", "done": True})

        task = asyncio.create_task(responder())
        collected: list[dict] = []
        async for chunk in submit_turn_streaming(
            ctx,
            turn_id=turn_id,
            payload={"turn_id": turn_id},
            timeout=5,
        ):
            collected.append(chunk)
        await task
        return collected

    chunks = _run(scenario())
    assert [c["response"] for c in chunks] == ["hel", "lo", ""]
    assert chunks[-1]["done"] is True
    assert not ctx.pending.has(turn_id)


def test_submit_turn_streaming_publishes_stuck_on_timeout() -> None:
    ctx = _StubCtx(_StubNATS())
    turn_id = "stream-timeout"

    async def scenario() -> bool:
        raised = False
        try:
            async for _ in submit_turn_streaming(
                ctx,
                turn_id=turn_id,
                payload={"turn_id": turn_id},
                timeout=0.01,
                mode="normal",
                user_waiting=True,
            ):
                pass
        except TimeoutError:
            raised = True
        return raised

    assert _run(scenario()), "submit_turn_streaming should re-raise TimeoutError"
    subjects = [s for s, _ in ctx.nc.published]
    assert CORTEX_STUCK in subjects
    stuck_payload = next(p for s, p in ctx.nc.published if s == CORTEX_STUCK)
    assert stuck_payload["mode"] == "normal"
    assert stuck_payload["user_waiting"] is True
    assert not ctx.pending.has(turn_id)


def test_submit_turn_streaming_publish_failure_raises_before_iteration() -> None:
    ctx = _StubCtx(_StubNATS(fail_on={CORTEX_TURN_REQUEST}))
    turn_id = "stream-publish-fail"

    async def scenario() -> bool:
        raised = False
        try:
            async for _ in submit_turn_streaming(
                ctx,
                turn_id=turn_id,
                payload={"turn_id": turn_id},
                timeout=1,
            ):
                pass
        except TurnPublishError:
            raised = True
        return raised

    assert _run(scenario()), "submit_turn_streaming should raise TurnPublishError"
    assert all(s != CORTEX_STUCK for s, _ in ctx.nc.published)
    assert not ctx.pending.has(turn_id)
