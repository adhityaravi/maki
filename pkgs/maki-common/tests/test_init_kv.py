"""Tests for ``init_kv`` default-seeding behaviour in ``maki_common.nats``.

Guards issue #456: ``init_kv`` used to seed defaults on ANY exception from
``kv.get``, which meant a transient blip during startup (mid-init NATS
reconnect, TLS renegotiation, request timeout during consumer rebalance)
would silently overwrite whatever live value was already persisted with
the stale default — no ERROR log, just an INFO ``"Seeded KV default"``
that read like normal first-boot behaviour.

Contract these tests lock in:

* An unset key (``KeyNotFoundError``) IS seeded with the default.
* An already-set key is left alone (no write).
* A transient error (``TimeoutError``, ``ConnectionClosedError``,
  ``NoServersError``, generic ``Exception``) does NOT trigger a write —
  the persisted value must survive.

Uses plain ``asyncio.run`` + ``assert`` so the maki-common test suite
stays pytest-asyncio-free (see ``test_futures.py``, ``test_nats_terminal.py``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import nats.errors
import nats.js.errors
from maki_common.nats import init_kv


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


# --- the #456 regression guards ----------------------------------------------


def test_init_kv_does_not_overwrite_on_timeout_error() -> None:
    """A transient TimeoutError must NOT trigger a write.

    This is the #456 scenario: NATS client mid-reconnect during
    JetStream handshake, ``kv.get`` times out — the old bare-except
    code would have written the stale default back on top of the
    live tuned value.
    """
    kv = _FakeKV(get_error=TimeoutError("kv.get timed out"))
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    _run(scenario())

    assert kv.puts == [], "transient TimeoutError must not clobber persisted value"


def test_init_kv_does_not_overwrite_on_no_servers_error() -> None:
    """``nats.errors.NoServersError`` on read is transient — leave KV alone."""
    kv = _FakeKV(get_error=nats.errors.NoServersError())
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    _run(scenario())

    assert kv.puts == [], "NoServersError must not clobber persisted value"


def test_init_kv_does_not_overwrite_on_generic_exception() -> None:
    """Any unexpected exception is treated as transient — no seed."""
    kv = _FakeKV(get_error=RuntimeError("something unexpected"))
    js = _FakeJS(kv)

    async def scenario() -> None:
        await init_kv(js, "cfg", defaults={"chat_model": "claude-sonnet-4-legacy"})

    _run(scenario())

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
