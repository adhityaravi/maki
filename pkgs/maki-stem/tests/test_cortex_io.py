"""Tests for stem's cortex response and heartbeat handlers (issue #394).

Covers the per-instance selective cancellation contract:

- ``_handle_response`` records ``turn_id → instance_id`` from every chunk so
  the heartbeat watcher can tell which restart affects which turn, and drops
  the mapping on the terminal ``done`` chunk.
- ``_handle_cortex_heartbeat`` cancels ONLY the turns owned by the restarted
  cortex instance (plus any unmapped turns whose ownership stem cannot yet
  prove), leaving turns owned by other healthy cortex replicas alone.

Prior to #394 the heartbeat handler called ``pending.cancel_all()`` on every
session flip, so restarting one cortex pod nuked in-flight turns on every
healthy pod in the fleet.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from maki_common.futures import PendingQueues
from maki_stem import cortex_io
from maki_stem.cortex_io import _handle_cortex_heartbeat, _handle_response


@dataclass
class _StubMsg:
    data: bytes


def _msg(payload: dict) -> _StubMsg:
    return _StubMsg(data=json.dumps(payload).encode())


def _run(coro):
    return asyncio.run(coro)


def _reset_module_state() -> None:
    cortex_io._cortex_sessions.clear()
    cortex_io._turn_to_instance.clear()


# --- _handle_response: turn → instance mapping -------------------------------


def test_handle_response_records_turn_instance_mapping() -> None:
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        pending.create("turn-1")
        await _handle_response(
            _msg({"turn_id": "turn-1", "response": "hi", "done": False, "instance_id": "cortex-a"}),
            pending,
        )
        assert cortex_io._turn_to_instance["turn-1"] == "cortex-a"

    _run(scenario())


def test_handle_response_drops_mapping_on_done_chunk() -> None:
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        pending.create("turn-1")
        await _handle_response(
            _msg({"turn_id": "turn-1", "response": "hi", "done": False, "instance_id": "cortex-a"}),
            pending,
        )
        assert "turn-1" in cortex_io._turn_to_instance
        await _handle_response(
            _msg({"turn_id": "turn-1", "response": "", "done": True, "instance_id": "cortex-a"}),
            pending,
        )
        assert "turn-1" not in cortex_io._turn_to_instance

    _run(scenario())


def test_handle_response_missing_instance_id_does_not_map() -> None:
    """Chunks from a pre-#394 cortex won't carry instance_id — don't crash, don't lie."""
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        pending.create("turn-1")
        await _handle_response(
            _msg({"turn_id": "turn-1", "response": "hi", "done": False}),
            pending,
        )
        assert "turn-1" not in cortex_io._turn_to_instance

    _run(scenario())


# --- _handle_cortex_heartbeat: selective cancellation ------------------------


async def _drain(queue: asyncio.Queue) -> list[dict]:
    out: list[dict] = []
    while not queue.empty():
        out.append(await queue.get())
    return out


def test_heartbeat_only_cancels_turns_owned_by_restarted_instance() -> None:
    """The core #394 regression: a restart on cortex-a must leave cortex-b's turn alone."""
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        # Seed sessions for both instances.
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-1", "instance_id": "cortex-a"}), pending)
        await _handle_cortex_heartbeat(_msg({"session_id": "s-b-1", "instance_id": "cortex-b"}), pending)

        # Two turns in flight: one owned by each cortex replica.
        q_a = pending.create("turn-owned-by-a")
        q_b = pending.create("turn-owned-by-b")
        cortex_io._turn_to_instance["turn-owned-by-a"] = "cortex-a"
        cortex_io._turn_to_instance["turn-owned-by-b"] = "cortex-b"

        # cortex-a restarts (new session_id).
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-2", "instance_id": "cortex-a"}), pending)

        # Only a's turn gets the cancellation sentinel; b's queue is untouched.
        drained_a = await _drain(q_a)
        drained_b = await _drain(q_b)
        assert drained_a == [{"response": "", "done": True, "cancelled": True}]
        assert drained_b == []
        # a's mapping is cleaned up; b's is preserved.
        assert "turn-owned-by-a" not in cortex_io._turn_to_instance
        assert cortex_io._turn_to_instance["turn-owned-by-b"] == "cortex-b"

    _run(scenario())


def test_heartbeat_cancels_unmapped_turns_alongside_owned_turns() -> None:
    """Unmapped turns (no first chunk yet) are cancelled too — the safer default."""
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-1", "instance_id": "cortex-a"}), pending)

        q_owned = pending.create("turn-owned")
        q_unmapped = pending.create("turn-unmapped")  # no _turn_to_instance entry
        cortex_io._turn_to_instance["turn-owned"] = "cortex-a"

        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-2", "instance_id": "cortex-a"}), pending)

        assert await _drain(q_owned) == [{"response": "", "done": True, "cancelled": True}]
        assert await _drain(q_unmapped) == [{"response": "", "done": True, "cancelled": True}]

    _run(scenario())


def test_heartbeat_first_sight_does_not_cancel() -> None:
    """The very first heartbeat from an instance is a session insert, not a restart."""
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        q = pending.create("turn-1")
        cortex_io._turn_to_instance["turn-1"] = "cortex-a"

        # First heartbeat from cortex-a — should just record the session, no cancel.
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-1", "instance_id": "cortex-a"}), pending)

        assert q.empty()
        assert cortex_io._turn_to_instance["turn-1"] == "cortex-a"

    _run(scenario())


def test_heartbeat_same_session_is_a_noop() -> None:
    """Repeated heartbeats with the same session_id don't cancel anything."""
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-1", "instance_id": "cortex-a"}), pending)
        q = pending.create("turn-1")
        cortex_io._turn_to_instance["turn-1"] = "cortex-a"

        # Same session_id → not a restart.
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-1", "instance_id": "cortex-a"}), pending)

        assert q.empty()
        assert cortex_io._turn_to_instance["turn-1"] == "cortex-a"

    _run(scenario())


def test_heartbeat_restart_with_no_pending_turns_is_silent() -> None:
    """A restart with no in-flight turns logs 'no pending turns' and returns."""
    _reset_module_state()
    pending = PendingQueues()

    async def scenario() -> None:
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-1", "instance_id": "cortex-a"}), pending)
        # No pending queues at all.
        await _handle_cortex_heartbeat(_msg({"session_id": "s-a-2", "instance_id": "cortex-a"}), pending)
        # Session was updated.
        assert cortex_io._cortex_sessions["cortex-a"] == "s-a-2"

    _run(scenario())
