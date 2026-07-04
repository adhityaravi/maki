"""Unified cortex-turn submission helpers.

Three call sites in stem (interactive chat, idle reflection, night work) all
follow the same skeleton: publish a turn request to cortex over NATS, await
one or more responses on a pending queue, and clean up on exit. This module
centralises that skeleton so:

- Timeout handling is uniform — every caller (idle included) publishes
  ``CORTEX_STUCK`` when cortex fails to respond within the timeout, giving
  immune a chance to rescue a wedged cortex regardless of who initiated
  the turn (issue #125).
- The ``turn_id`` prefix, publish call, and queue lifecycle are declared
  in exactly one place.

Two variants:

- :func:`submit_turn_single` — publish, await a single response payload,
  return it. Used by idle and work.
- :func:`submit_turn_streaming` — publish, yield each chunk as it arrives,
  stop once ``done`` is set. Used by interactive chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from maki_common.subjects import CORTEX_STUCK, CORTEX_TURN_REQUEST

if TYPE_CHECKING:
    # Type-only import to avoid the runtime cycle: ``loops.idle`` and
    # ``loops.work`` both import this module, and ``loops.__init__``
    # eagerly loads them from ``StemContext``'s home package. Guarding the
    # annotation keeps ``turn.py`` importable during that chain.
    from maki_stem.loops.base import StemContext

log = logging.getLogger(__name__)


class TurnPublishError(Exception):
    """Raised when the NATS publish for a cortex turn fails.

    The cortex turn never started, so callers that track per-work-item
    failure counters (see issue #284 in ``loops/work.py``) should treat
    this as an infrastructure failure — not as an attempted-but-failed
    turn — and refrain from counting it against the underlying item.

    Wraps the underlying NATS exception via ``__cause__``.
    """


def new_turn_id(prefix: str) -> str:
    """Generate a turn id with a caller-supplied prefix.

    Centralising this keeps the ``<prefix>-<hex>`` shape identical across
    every call site so log filtering and immune's stuck-turn correlation
    stay reliable.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _publish_stuck(
    ctx: StemContext,
    *,
    turn_id: str,
    mode: str,
    timeout: int,
    user_waiting: bool,
) -> None:
    """Publish CORTEX_STUCK to notify immune of a wedged cortex.

    Best-effort — a NATS failure here is logged and swallowed so the caller's
    ``TimeoutError`` still surfaces (recording the stuck event is a courtesy
    to immune, not a precondition for the timeout to propagate).
    """
    try:
        await ctx.nc.publish(
            CORTEX_STUCK,
            json.dumps(
                {
                    "turn_id": turn_id,
                    "mode": mode,
                    "timeout_seconds": timeout,
                    "user_waiting": user_waiting,
                }
            ).encode(),
        )
    except Exception:
        log.warning(
            "Failed to publish CORTEX_STUCK after turn timeout",
            extra={"turn_id": turn_id, "mode": mode},
            exc_info=True,
        )


async def submit_turn_single(
    ctx: StemContext,
    *,
    turn_id: str,
    payload: dict,
    timeout: int,
    mode: str,
    user_waiting: bool = False,
) -> dict:
    """Publish a single-shot turn, await one response, handle timeout uniformly.

    Publishes ``CORTEX_STUCK`` on timeout for every caller so immune can
    rescue a wedged cortex regardless of who initiated the turn.

    Raises :class:`TimeoutError` if cortex does not respond within *timeout*
    seconds — the stuck signal is published just before the exception
    propagates, so callers only need to catch ``TimeoutError`` and decide
    what to do about the failed turn itself.

    Raises :class:`TurnPublishError` (wrapping the underlying NATS exception)
    when the initial publish fails. Callers that distinguish "infra never
    delivered the request" from "cortex received but hung/errored" should
    catch this separately.
    """
    async with ctx.pending.session(turn_id) as queue:
        try:
            await ctx.nc.publish(CORTEX_TURN_REQUEST, json.dumps(payload).encode())
        except Exception as e:
            raise TurnPublishError(f"failed to publish turn {turn_id}") from e
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            await _publish_stuck(
                ctx,
                turn_id=turn_id,
                mode=mode,
                timeout=timeout,
                user_waiting=user_waiting,
            )
            raise


async def submit_turn_streaming(
    ctx: StemContext,
    *,
    turn_id: str,
    payload: dict,
    timeout: int,
    mode: str = "normal",
    user_waiting: bool = True,
) -> AsyncIterator[dict]:
    """Publish a streaming turn, yield each chunk as it arrives.

    The timeout applies *per chunk* — same semantics as the pre-refactor
    inline loop. Publishes ``CORTEX_STUCK`` on timeout and re-raises
    :class:`TimeoutError`.

    Iteration stops after the first chunk whose ``done`` field is truthy.

    Raises :class:`TurnPublishError` (wrapping the underlying NATS exception)
    when the initial publish fails.
    """
    async with ctx.pending.session(turn_id) as queue:
        try:
            await ctx.nc.publish(CORTEX_TURN_REQUEST, json.dumps(payload).encode())
        except Exception as e:
            raise TurnPublishError(f"failed to publish turn {turn_id}") from e
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                await _publish_stuck(
                    ctx,
                    turn_id=turn_id,
                    mode=mode,
                    timeout=timeout,
                    user_waiting=user_waiting,
                )
                raise
            yield data
            if data.get("done"):
                return
