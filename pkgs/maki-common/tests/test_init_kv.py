"""Tests for ``init_kv`` default-seeding behaviour in ``maki_common.nats``.

Guards issues #456 and #479: ``init_kv`` used to seed defaults on ANY
exception from ``kv.get``, so a transient blip during startup (mid-init
NATS reconnect, TLS renegotiation, request timeout during consumer
rebalance) would silently overwrite whatever live value was already
persisted with the stale default — no ERROR log, just an INFO
``"Seeded KV default"`` that read like normal first-boot behaviour.
#456's first pass narrowed the write to ``KeyNotFoundError`` but kept
a log-and-continue swallow for other errors; #479 removed the swallow
so init failures propagate to the caller (and ultimately k8s) instead
of being hidden.

Contract these tests lock in:

* An unset key (``KeyNotFoundError``) IS seeded with the default.
* An already-set key is left alone (no write).
* A transient error (``TimeoutError``, ``NoServersError``, generic
  ``Exception``) propagates unchanged — the persisted value survives
  AND the caller sees the failure, so startup can crash cleanly rather
  than continuing with a half-initialised bucket.

Uses plain ``asyncio.run`` + ``assert`` so the maki-common test suite
stays pytest-asyncio-free (see ``test_futures.py``, ``test_nats_terminal.py``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import nats.errors
import nats.js.errors
from maki_common.nats import init_kv, init_kv_with_retry


def _run(coro):
    return asyncio.run(coro)


class _FakeKV:
    """Minimal KV stub tracking get/put calls and simulating errors."""

    def __init__(
        self,
        stored: dict[str, bytes] | None = None,
        get_error: BaseException | None = None,
    ) -> None:
        self.stored: dict[str, bytes] = dict(stored or {})
        self.get_error = get_error
        self.puts: list[tuple[str, bytes]] = []

    async def get(self, key: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        if key not in self.stored:
            raise nats.js.errors.KeyNotFoundError()

        class _Entry:
            def __init__(self, value: bytes) -> None:
                self.value = value

        return _Entry(self.stored[key])

    async def put(self, key: str, value: bytes) -> None:
        self.puts.append((key, value))
        self.stored[key] = value


class _FakeJS:
    """JetStream stub that returns a preconfigured KV bucket."""

    def __init__(self, kv: _FakeKV) -> None:
        self._kv = kv
        self.key_value_calls = 0
        self.create_calls = 0

    async def key_value(self, bucket: str) -> _FakeKV:
        self.key_value_calls += 1
        return self._kv

    async def create_key_value(self, bucket: str) -> _FakeKV:  # pragma: no cover
        self.create_calls += 1
        return self._kv


# --- happy path: unset key gets seeded ---------------------------------------


def test_init_kv_seeds_missing_key() -> None:
    """A key that genuinely doesn't exist (KeyNotFoundError) is seeded."""
    kv = _FakeKV(stored={})
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4"})

    _run(scenario())

    assert len(kv.puts) == 1
    key, value = kv.puts[0]
    assert key == "chat_model"
    assert json.loads(value.decode()) == "claude-sonnet-4"


# --- happy path: existing value preserved ------------------------------------


def test_init_kv_leaves_existing_value_alone() -> None:
    """A key that already has a value is NOT overwritten."""
    kv = _FakeKV(stored={"chat_model": json.dumps("claude-opus-4-7").encode()})
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    _run(scenario())

    assert kv.puts == [], "must not overwrite an already-set key"
    assert json.loads(kv.stored["chat_model"].decode()) == "claude-opus-4-7"


# --- the #456 / #479 regression guards ---------------------------------------


def _assert_raises(exc_type: type[BaseException], coro) -> BaseException:
    """Run ``coro`` and assert it raises ``exc_type``. Return the exception."""
    try:
        _run(coro)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}, got no exception")


def test_init_kv_propagates_timeout_error() -> None:
    """A transient TimeoutError propagates AND leaves the persisted value alone.

    This is the #456 / #479 scenario: NATS client mid-reconnect during
    JetStream handshake, ``kv.get`` times out. The old bare-except code
    would have written the stale default back on top of the live tuned
    value; the #456 log-and-continue variant would have booted with an
    unread bucket; #479 makes the failure visible to the caller so k8s
    can restart the pod and try again against a healthy NATS.
    """
    kv = _FakeKV(get_error=TimeoutError("kv.get timed out"))
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    _assert_raises(TimeoutError, scenario())

    assert kv.puts == [], "transient TimeoutError must not clobber persisted value"


def test_init_kv_propagates_no_servers_error() -> None:
    """``nats.errors.NoServersError`` on read propagates unchanged."""
    kv = _FakeKV(get_error=nats.errors.NoServersError())
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    _assert_raises(nats.errors.NoServersError, scenario())

    assert kv.puts == [], "NoServersError must not clobber persisted value"


def test_init_kv_propagates_generic_exception() -> None:
    """Any unexpected exception propagates — no swallow, no seed."""
    kv = _FakeKV(get_error=RuntimeError("something unexpected"))
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    exc = _assert_raises(RuntimeError, scenario())
    assert str(exc) == "something unexpected"

    assert kv.puts == [], "unknown exception must not clobber persisted value"


def test_init_kv_seeds_only_missing_keys_in_mixed_batch() -> None:
    """When some keys exist and some don't, only the missing ones get seeded."""
    kv = _FakeKV(stored={"chat_model": json.dumps("claude-opus-4-7").encode()})
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(
            js,
            "cfg",
            defaults={
                "chat_model": "claude-sonnet-4-legacy",  # already set, skip
                "retention_days": 30,  # missing, seed
                "max_tokens": 4096,  # missing, seed
            },
        )

    _run(scenario())

    seeded = {k: json.loads(v.decode()) for k, v in kv.puts}
    assert seeded == {"retention_days": 30, "max_tokens": 4096}
    # And the pre-existing tuned value is still intact.
    assert json.loads(kv.stored["chat_model"].decode()) == "claude-opus-4-7"


# --- init_kv_with_retry: the #758 cold-start retry wrapper ------------------
#
# These guard the inverse of the "propagate" contract above: the outer
# wrapper MUST ride out transient blips without crashing the pod, but MUST
# still re-raise once attempts are exhausted so a genuinely dead NATS still
# surfaces to k8s. See issue #758 — cortex sat in CrashLoopBackOff for ~9.5h
# because one 5s JetStream timeout at boot killed the process every time k8s
# re-ran it.


class _FlakyJS:
    """JetStream stub whose ``key_value`` fails N times, then succeeds.

    Models the #758 scenario: NATS connect works (``connect_nats`` has its
    own retry) but the first JetStream ``stream_info`` request after the TCP
    handshake times out while a leader election / disk flush completes. A
    retry a few seconds later succeeds.
    """

    def __init__(self, kv: _FakeKV, fail_first: int, exc: BaseException) -> None:
        self._kv = kv
        self._fail_first = fail_first
        self._exc = exc
        self.key_value_calls = 0
        self.create_calls = 0

    async def key_value(self, bucket: str) -> _FakeKV:
        self.key_value_calls += 1
        if self.key_value_calls <= self._fail_first:
            raise self._exc
        return self._kv

    async def create_key_value(self, bucket: str) -> _FakeKV:  # pragma: no cover
        self.create_calls += 1
        return self._kv


def test_init_kv_with_retry_recovers_from_transient_timeout() -> None:
    """Two TimeoutErrors in a row then success → one healthy KV handle returned.

    The #758 happy path: ~5s of JetStream unavailability at cold start, which
    before this wrapper crashed the pod and cost 5+ min of exp-backoff. Now
    the wrapper sleeps ~2s + ~4s and the third attempt wins.
    """
    kv = _FakeKV(stored={})
    js = _FlakyJS(kv, fail_first=2, exc=nats.errors.TimeoutError())

    async def scenario() -> _FakeKV:
        # jitter=0 for test determinism; tiny delays so the test is fast.
        return await init_kv_with_retry(js, "cfg", attempts=5, base_delay=0.001, max_delay=0.01, jitter=0)

    result = _run(scenario())
    assert result is kv
    assert js.key_value_calls == 3  # 2 failures + 1 success


def test_init_kv_with_retry_raises_after_exhausting_attempts() -> None:
    """A permanently-dead NATS still surfaces to k8s after N attempts.

    Defense against the "retry forever and silently mask a config bug"
    anti-pattern. If every attempt fails we re-raise unchanged — kubelet
    then enters CrashLoopBackOff as intended and the real error shows up
    in ``kubectl logs --previous``.
    """
    kv = _FakeKV(stored={})
    # 999 > attempts, so every call fails.
    js = _FlakyJS(kv, fail_first=999, exc=nats.errors.TimeoutError("nats: timeout"))

    async def scenario() -> None:
        await init_kv_with_retry(js, "cfg", attempts=3, base_delay=0.001, max_delay=0.01, jitter=0)

    exc = _assert_raises(nats.errors.TimeoutError, scenario())
    assert "timeout" in str(exc).lower()
    assert js.key_value_calls == 3  # all attempts consumed, no silent extra tries


def test_init_kv_with_retry_first_attempt_succeeds_no_sleep() -> None:
    """Happy-path cold start: NATS healthy, one call, no retry delay burned."""
    kv = _FakeKV(stored={})
    js = _FlakyJS(kv, fail_first=0, exc=RuntimeError("should never raise"))

    async def scenario() -> _FakeKV:
        return await init_kv_with_retry(js, "cfg", attempts=5, base_delay=1.0, max_delay=10.0, jitter=0)

    result = _run(scenario())
    assert result is kv
    assert js.key_value_calls == 1  # single attempt, no retry sleep incurred


def test_init_kv_with_retry_passes_defaults_through() -> None:
    """Defaults handed to the wrapper reach init_kv on the successful attempt.

    Guards against a refactor that drops the ``defaults`` kwarg on the inner
    call — immune relies on this to seed DEFAULT_CONFIG at cold start.
    """
    kv = _FakeKV(stored={})
    js = _FlakyJS(kv, fail_first=1, exc=nats.errors.TimeoutError())

    async def scenario() -> None:
        await init_kv_with_retry(
            js,
            "cfg",
            defaults={"chat_model": "claude-sonnet-4"},
            attempts=3,
            base_delay=0.001,
            max_delay=0.01,
            jitter=0,
        )

    _run(scenario())

    assert js.key_value_calls == 2  # 1 fail + 1 success
    seeded = {k: json.loads(v.decode()) for k, v in kv.puts}
    assert seeded == {"chat_model": "claude-sonnet-4"}
