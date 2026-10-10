"""Dedup helper for maki-ears message handlers.

All user-facing entry points (chat, `!trade`, `!loop`, `#maki-immune`)
claim their message-id in the ``maki-ears-dedup`` KV bucket before doing
any real work — so a blue/green rollout can't process the same Discord
event twice. The pre-fix pattern was::

    try:
        await _dedup_kv.create(msg_key, b"1")
    except Exception:
        log.info("Dedup: already claimed")
        return

That swallowed *every* exception as if it were a real duplicate. A
transient NATS blip (``TimeoutError``, ``NoRespondersError``, a wedged
bucket) then looked identical to "another instance got here first" —
Adi typed something, saw no reaction, no reply, and the log line lied
about why. See issue #416.

:func:`claim_or_skip` separates the two cases:

* ``KeyWrongLastSequenceError`` — the key really exists. Return ``False``
  and the caller skips silently as before.
* Anything else — the KV / NATS is unhealthy. Re-raise. The caller
  decides whether to fail-open (process anyway, WARN loudly) or
  fail-loud (reply "try again"). With a single ears replica today,
  fail-open is the right call — dedup is defence-in-depth for
  blue/green, not the source of truth.
"""

from __future__ import annotations

import logging
from typing import Any

from nats.js.errors import KeyWrongLastSequenceError

log = logging.getLogger(__name__)


async def claim_or_skip(dedup_kv: Any, message_id: str, label: str) -> bool:
    """Claim ``msg.<message_id>`` in the dedup KV.

    Returns:
        True if this instance just claimed the key (caller should proceed).
        False if the key already exists — another instance already handled
        this message and the caller should skip silently.

    Raises:
        Any exception other than :class:`KeyWrongLastSequenceError` — meaning
        the KV / NATS itself is unhealthy, not that the message is a
        duplicate. Callers must handle this rather than treat it as a skip.
    """
    try:
        await dedup_kv.create(f"msg.{message_id}", b"1")
        return True
    except KeyWrongLastSequenceError:
        log.info(f"Dedup: {label} already claimed by another instance", extra={"message_id": message_id})
        return False
