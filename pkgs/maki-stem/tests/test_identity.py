"""Identity lookup must share the bootstrap fallback across loops and chat."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maki_stem.identity import DEFAULT_IDENTITY, KV_KEY
from maki_stem.loops.base import load_identity


def test_load_identity_reads_runtime_override() -> None:
    kv = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(value=b"Runtime identity")))
    assert asyncio.run(load_identity(kv)) == "Runtime identity"
    kv.get.assert_awaited_once_with(KV_KEY)


def test_load_identity_uses_bootstrap_default_on_lookup_error() -> None:
    kv = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("KV unavailable")))
    assert asyncio.run(load_identity(kv)) == DEFAULT_IDENTITY


def test_load_identity_preserves_caller_fallback_on_decode_error() -> None:
    kv = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(value=b"\xff")))
    assert asyncio.run(load_identity(kv, "Caller fallback")) == "Caller fallback"
