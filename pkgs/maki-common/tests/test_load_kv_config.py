"""Tests for ``load_kv_config`` in ``maki_common.nats``.

Covers three regimes:

* The happy path (valid JSON in the bucket, or key genuinely unset).
* The #638 encoding-mismatch escalation: a raw-encoded string that
  can't be ``json.loads``'d used to silently revert to the seed default;
  reader logging escalated the split-brain to ERROR.
* The #757 self-heal / one-shot migration: an unparseable stored value
  is now rewritten as valid JSON via CAS at the observed revision, and
  the recovered string is returned to this reader instead of the seed
  default. Pre-existing raw values written before the #638 fix landed
  otherwise stayed stuck at "default" forever, spamming ERROR every
  read tick — see the issue for the live-symptom trace.

Uses plain ``asyncio.run`` + ``assert`` so the maki-common test suite
stays pytest-asyncio-free (see ``test_init_kv.py``, ``test_futures.py``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import nats.js.errors
from maki_common.nats import load_kv_config


def _run(coro):
    return asyncio.run(coro)


class _Entry:
    def __init__(self, value: bytes, revision: int = 1) -> None:
        self.value = value
        self.revision = revision


class _FakeKV:
    """Minimal KV stub tracking get/put/update calls and simulating errors.

    ``stored`` maps key -> (raw_bytes, revision). Updates bump the
    revision on success. ``update_error`` and ``get_error`` (per-key or
    global) let each test rig one branch's failure mode.
    """

    def __init__(
        self,
        stored: dict[str, tuple[bytes, int]] | None = None,
        get_error: BaseException | None = None,
        update_error: BaseException | None = None,
    ) -> None:
        self.stored: dict[str, tuple[bytes, int]] = dict(stored or {})
        self.get_error = get_error
        self.update_error = update_error
        self.updates: list[tuple[str, bytes, int]] = []
        self.puts: list[tuple[str, bytes]] = []

    async def get(self, key: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        if key not in self.stored:
            raise nats.js.errors.KeyNotFoundError()
        value, revision = self.stored[key]
        return _Entry(value, revision)

    async def update(self, key: str, value: bytes, revision: int) -> None:
        self.updates.append((key, value, revision))
        if self.update_error is not None:
            raise self.update_error
        stored_revision = self.stored.get(key, (b"", 0))[1]
        if stored_revision != revision:
            raise RuntimeError(f"CAS mismatch: expected {stored_revision}, got {revision}")
        self.stored[key] = (value, revision + 1)

    async def put(self, key: str, value: bytes) -> None:
        self.puts.append((key, value))
        current_revision = self.stored.get(key, (b"", 0))[1]
        self.stored[key] = (value, current_revision + 1)


# --- happy paths -------------------------------------------------------------


def test_load_kv_config_returns_valid_json_value() -> None:
    """A key stored as valid JSON round-trips through the reader."""
    kv = _FakeKV(stored={"chat_model": (json.dumps("claude-opus-4-7").encode(), 3)})

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"chat_model": "seed-default"})

    config = _run(scenario())
    assert config == {"chat_model": "claude-opus-4-7"}
    # No self-heal writes on the happy path.
    assert kv.updates == []


def test_load_kv_config_returns_default_for_missing_key() -> None:
    """A genuinely-unset key uses the caller's default without any writes."""
    kv = _FakeKV(stored={})

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"chat_model": "seed-default", "retention": 30})

    config = _run(scenario())
    assert config == {"chat_model": "seed-default", "retention": 30}
    assert kv.updates == []
    assert kv.puts == []


def test_load_kv_config_returns_default_on_transient_read_error() -> None:
    """A transient ``kv.get`` failure falls back to the default silently (#369)."""
    kv = _FakeKV(get_error=TimeoutError("kv.get timed out"))

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"chat_model": "seed-default"})

    config = _run(scenario())
    assert config == {"chat_model": "seed-default"}
    assert kv.updates == []


# --- #757 self-heal on read --------------------------------------------------


def test_load_kv_config_self_heals_raw_string_via_cas() -> None:
    """Pre-#638 raw ``value.encode()`` is rewritten as valid JSON and returned.

    Guards the #757 gap: after #638 fixed the writers, existing raw
    values in the bucket stayed unparseable forever because nothing
    ever wrote back to them. The reader now migrates on the spot.
    """
    raw = b"claude-opus-4-7"  # pre-#638: str.encode(), no JSON framing
    kv = _FakeKV(stored={"chat_model": (raw, 7)})

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"chat_model": "seed-default"})

    config = _run(scenario())

    # The recovered string — not the seed default — is returned to this reader.
    assert config == {"chat_model": "claude-opus-4-7"}
    # And it was rewritten as valid JSON via CAS at the observed revision.
    assert len(kv.updates) == 1
    key, value, revision = kv.updates[0]
    assert key == "chat_model"
    assert json.loads(value.decode()) == "claude-opus-4-7"
    assert revision == 7
    # The bucket now round-trips through a plain reader.
    stored_bytes, new_revision = kv.stored["chat_model"]
    assert json.loads(stored_bytes.decode()) == "claude-opus-4-7"
    assert new_revision == 8


def test_load_kv_config_self_heal_survives_cas_failure() -> None:
    """If the CAS write races/fails, the reader still gets the recovered value.

    A concurrent healthy writer may have moved the revision forward
    between our ``get`` and ``update``. The migration write loses, but
    the recovered string is still what the raw bytes represented, so
    returning it is safe — and next reader will retry.
    """
    raw = b"claude-opus-4-7"
    kv = _FakeKV(
        stored={"chat_model": (raw, 7)},
        update_error=RuntimeError("wrong last sequence"),
    )

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"chat_model": "seed-default"})

    config = _run(scenario())

    assert config == {"chat_model": "claude-opus-4-7"}
    # We attempted the write.
    assert len(kv.updates) == 1
    # But the bucket still holds the raw value (update failed).
    assert kv.stored["chat_model"] == (raw, 7)


def test_load_kv_config_self_heal_non_string_default_returns_default() -> None:
    """Unparseable value under a non-str default falls back to default.

    We can't guess the intended type, so returning the raw text could
    crash downstream. But we still rewrite the KV to the JSON-encoded
    default so the ERROR spam stops — the observable behaviour was
    already "consumer sees default".
    """
    raw = b"thirty"  # neither JSON nor a valid int literal
    kv = _FakeKV(stored={"retention": (raw, 4)})

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"retention": 30})

    config = _run(scenario())

    assert config == {"retention": 30}
    assert len(kv.updates) == 1
    key, value, revision = kv.updates[0]
    assert key == "retention"
    assert json.loads(value.decode()) == 30
    assert revision == 4


def test_load_kv_config_self_heal_skips_non_utf8_bytes() -> None:
    """Bytes that aren't even valid UTF-8 have no honest salvage.

    We log-and-default WITHOUT rewriting — clobbering random bytes with
    the default is guessing at intent, and the source of the bytes
    (some future writer? A partial write?) is worth surfacing rather
    than papering over.
    """
    raw = b"\xff\xfe\xfd\xfc"  # invalid UTF-8
    kv = _FakeKV(stored={"chat_model": (raw, 2)})

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(kv, {"chat_model": "seed-default"})

    config = _run(scenario())

    assert config == {"chat_model": "seed-default"}
    assert kv.updates == []


def test_load_kv_config_self_heals_multiple_keys_in_one_pass() -> None:
    """Every unparseable key in the defaults dict is migrated on the same tick.

    Guards the "at least two unparseable keys, two ERROR cadences" note
    on #757 — whatever the second offender is, walking all defaults
    each read drains the bucket in one pass.
    """
    kv = _FakeKV(
        stored={
            "chat_model": (b"claude-opus-4-7", 1),
            "loop_interval": (b"60", 2),  # parses as JSON int, no heal needed
            "greeting": (b"hello world", 3),  # raw string, needs heal
        }
    )

    async def scenario() -> dict[str, Any]:
        return await load_kv_config(
            kv,
            {"chat_model": "seed", "loop_interval": 30, "greeting": "hi"},
        )

    config = _run(scenario())

    assert config == {
        "chat_model": "claude-opus-4-7",
        "loop_interval": 60,
        "greeting": "hello world",
    }
    # Two migrations: chat_model and greeting. loop_interval was already JSON.
    healed_keys = sorted(k for k, _, _ in kv.updates)
    assert healed_keys == ["chat_model", "greeting"]
