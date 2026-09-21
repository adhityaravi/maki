"""Tests for ``kv_acquire_lease`` corrupt-value handling in ``maki_common.nats``.

Guards issue #644: the pre-fix implementation swallowed
``json.JSONDecodeError`` under a broad ``except Exception: return False``,
which permanently bricked any lease key whose stored value became
non-JSON (partial JetStream write, manual ``nats kv put`` typo, schema
mismatch). No log line, no metric, no reflex — the fleet just stopped
firing that loop or lost its Discord leader with no automatic recovery.

Contract these tests lock in:

* Corrupt JSON at the stored key is logged (with the key + revision,
  never the raw value) AND force-rewritten via CAS ``kv.update``. The
  caller returns True so the lease self-heals on the very next call.
* If the CAS force-rewrite loses the race (concurrent writer already
  fixed the value), we return False cleanly — the next call will read
  valid JSON and take the normal path.
* Non-UTF-8 bytes trip the same path (``UnicodeDecodeError``).
* The happy paths — missing key, fresh claim, expired claim, renewal —
  still behave exactly as before the fix.

Uses plain ``asyncio.run`` + ``assert`` to stay pytest-asyncio-free
(see ``test_init_kv.py``, ``test_futures.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import nats.js.errors
from maki_common.nats import kv_acquire_lease


def _run(coro):
    return asyncio.run(coro)


class _Entry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class _FakeKV:
    """Minimal KV stub for lease-CAS scenarios."""

    def __init__(
        self,
        stored: bytes | None = None,
        revision: int = 1,
        update_error: BaseException | None = None,
        create_error: BaseException | None = None,
        get_error: BaseException | None = None,
    ) -> None:
        self.stored = stored
        self.revision = revision
        self.update_error = update_error
        self.create_error = create_error
        self.get_error = get_error
        self.updates: list[tuple[str, bytes, int]] = []
        self.creates: list[tuple[str, bytes]] = []

    async def get(self, key: str) -> _Entry:
        if self.get_error is not None:
            raise self.get_error
        if self.stored is None:
            raise nats.js.errors.KeyNotFoundError()
        return _Entry(self.stored, self.revision)

    async def update(self, key: str, value: bytes, revision: int) -> None:
        self.updates.append((key, value, revision))
        if self.update_error is not None:
            raise self.update_error
        self.stored = value
        self.revision = revision + 1

    async def create(self, key: str, value: bytes) -> None:
        self.creates.append((key, value))
        if self.create_error is not None:
            raise self.create_error
        self.stored = value
        self.revision = 1


# --- #644 regression guards --------------------------------------------------


def test_corrupt_json_is_force_rewritten_via_cas() -> None:
    """The core #644 fix: corrupt JSON at the key gets force-rewritten.

    Pre-fix, ``json.loads`` would raise ``JSONDecodeError``, fall through
    to ``except Exception: return False``, and the key would be
    permanently unclaimable because every write branch was gated by a
    successful decode. The fix catches the decode error explicitly,
    logs it, and CAS-updates at the observed revision.
    """
    kv = _FakeKV(stored=b'{"instance": "old", "cla', revision=42)

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me")

    got = _run(scenario())
    assert got is True, "corrupt lease must self-heal, not stay stuck at False"

    assert len(kv.updates) == 1, "must force-rewrite via CAS"
    key, value, revision = kv.updates[0]
    assert key == "ears.leader"
    assert revision == 42, "CAS revision must match the corrupt entry's revision"
    payload = json.loads(value.decode())
    assert payload["instance"] == "me"


def test_corrupt_json_logs_key_and_revision_never_raw_value(
    caplog: Any = None,
) -> None:
    """Log the key + revision + error type, but NEVER the raw value.

    Future callers may put secrets in the claim payload. #644 asked for
    an ERROR log that a human can grep, not a leak.
    """
    kv = _FakeKV(stored=b"totally not json", revision=7)

    handler_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record)

    logger = logging.getLogger("maki_common.nats")
    handler = _Capture(level=logging.ERROR)
    logger.addHandler(handler)
    try:

        async def scenario() -> bool:
            return await kv_acquire_lease(kv, "loop.stem.idle", 5.0, "me")

        _run(scenario())
    finally:
        logger.removeHandler(handler)

    error_records = [r for r in handler_records if r.levelno == logging.ERROR]
    assert error_records, "expected an ERROR log on corrupt JSON"
    rec = error_records[0]
    assert rec.key == "loop.stem.idle"  # type: ignore[attr-defined]
    assert rec.revision == 7  # type: ignore[attr-defined]
    assert rec.error_type == "JSONDecodeError"  # type: ignore[attr-defined]
    # The raw value ("totally not json") must not appear anywhere in the log.
    formatted = rec.getMessage()
    assert "totally not json" not in formatted
    for _, val in rec.__dict__.items():
        assert val != b"totally not json", "raw value must not be logged"


def test_corrupt_json_returns_false_on_cas_race_loss() -> None:
    """If a concurrent writer already fixed the corrupt value, bail cleanly.

    ``kv.update`` at the corrupt revision fails (someone else won the
    CAS race), meaning the value at that revision is no longer the
    corrupt one we saw. Return False; the next call re-reads and sees
    valid JSON.
    """

    class _CasConflict(Exception):
        """Stand-in for nats-py's wrong-last-sequence CAS conflict."""

    kv = _FakeKV(
        stored=b"garbage{",
        revision=99,
        update_error=_CasConflict("wrong last sequence"),
    )

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me")

    got = _run(scenario())
    assert got is False, "must not claim the lease when CAS force-rewrite loses"
    assert len(kv.updates) == 1, "must have attempted exactly one CAS force-rewrite"


def test_non_utf8_bytes_trip_the_same_self_heal_path() -> None:
    """``UnicodeDecodeError`` from a non-UTF-8 payload uses the same recovery."""
    kv = _FakeKV(stored=b"\xff\xfe\x00\x01 not utf-8", revision=3)

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me")

    got = _run(scenario())
    assert got is True, "non-UTF-8 corruption must self-heal too"
    assert len(kv.updates) == 1
    assert kv.updates[0][2] == 3, "CAS at the observed revision"


# --- happy paths still work --------------------------------------------------


def test_missing_key_creates_new_claim() -> None:
    """KeyNotFoundError takes the ``kv.create`` path and returns True."""
    kv = _FakeKV()  # stored=None → KeyNotFoundError from _FakeKV.get

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me")

    got = _run(scenario())
    assert got is True
    assert len(kv.creates) == 1
    assert len(kv.updates) == 0


def test_expired_lease_is_taken_over() -> None:
    """An expired lease from someone else is taken over via CAS update."""
    old = json.dumps({"instance": "them", "claimed_at": 0.0}).encode()
    kv = _FakeKV(stored=old, revision=5)

    async def scenario() -> bool:
        # ttl=1s, and claimed_at=0 was 1970 — obviously expired
        return await kv_acquire_lease(kv, "ears.leader", 1.0, "me")

    got = _run(scenario())
    assert got is True
    assert len(kv.updates) == 1
    assert kv.updates[0][2] == 5


def test_fresh_lease_from_other_instance_blocks_claim() -> None:
    """A fresh lease held by someone else means we don't claim, no writes."""
    import time as _time  # noqa: PLC0415

    fresh = json.dumps({"instance": "them", "claimed_at": _time.time()}).encode()
    kv = _FakeKV(stored=fresh, revision=5)

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me")

    got = _run(scenario())
    assert got is False
    assert kv.updates == []


def test_renew_own_fresh_lease_when_allowed() -> None:
    """With ``allow_renew=True``, the current holder refreshes their claim."""
    import time as _time  # noqa: PLC0415

    fresh = json.dumps({"instance": "me", "claimed_at": _time.time()}).encode()
    kv = _FakeKV(stored=fresh, revision=5)

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me", allow_renew=True)

    got = _run(scenario())
    assert got is True
    assert len(kv.updates) == 1


def test_transient_get_error_returns_false_without_writing() -> None:
    """A generic transient read failure returns False and does NOT write.

    Someone may still be the holder; we simply couldn't read the value
    this tick. Silently rewriting on a transient read would be #274 /
    #479 all over again.
    """
    kv = _FakeKV(get_error=TimeoutError("kv.get timed out"))

    async def scenario() -> bool:
        return await kv_acquire_lease(kv, "ears.leader", 30.0, "me")

    got = _run(scenario())
    assert got is False
    assert kv.updates == []
    assert kv.creates == []
