"""Tests for terminal-NATS-error classification in ``maki_common.nats``.

Guards the contract issue #470 depends on:

* ``connect_nats`` raises :class:`NatsTerminalError` (not the generic
  Exception path) for permanent auth/TLS failures, so recall's lifespan
  can catch it distinctly and expose ``nats_terminal=true`` on /health.
* The classifier accepts both the client-side exception classes and the
  raw server -ERR message shape (``nats.errors.Error("nats: 'Authorization
  Violation'")``) — the two shapes both show up in the wild depending on
  where the failure gets caught in nats-py.
* Transient failures (``ConnectionRefusedError``, generic ``OSError``)
  still take the backoff path — the whole point is to NOT terminal-classify
  the cold-start race with maki-nerve-nats.

Uses plain ``asyncio.run`` + ``assert`` so the maki-common test suite stays
pytest-asyncio-free (see ``test_futures.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import nats.errors
from maki_common.nats import NatsTerminalError, _classify_terminal_nats_error, connect_nats


def _run(coro):
    return asyncio.run(coro)


# --- classifier ---------------------------------------------------------------


def test_classify_authorization_error_by_type() -> None:
    exc = nats.errors.AuthorizationError()
    assert _classify_terminal_nats_error(exc) == "AuthorizationError"


def test_classify_secure_conn_required_by_type() -> None:
    exc = nats.errors.SecureConnRequiredError()
    assert _classify_terminal_nats_error(exc) == "SecureConnRequiredError"


def test_classify_invalid_credentials_by_type() -> None:
    exc = nats.errors.InvalidUserCredentialsError()
    assert _classify_terminal_nats_error(exc) == "InvalidUserCredentialsError"


def test_classify_authorization_violation_by_message() -> None:
    # The exact shape nats-py surfaces when the server sends -ERR
    # 'Authorization Violation': a bare Error with the phrase embedded.
    exc = nats.errors.Error("nats: 'Authorization Violation'")
    assert _classify_terminal_nats_error(exc) == "authorization_violation"


def test_classify_authorization_timeout_by_message() -> None:
    exc = nats.errors.Error("nats: 'Authorization Timeout'")
    assert _classify_terminal_nats_error(exc) == "authorization_timeout"


def test_classify_tls_required_by_message() -> None:
    exc = nats.errors.Error("nats: 'TLS Required'")
    assert _classify_terminal_nats_error(exc) == "tls_required"


def test_classify_message_match_is_case_insensitive() -> None:
    # Server capitalisation has drifted between versions; the classifier
    # normalises so a case flip doesn't silently reintroduce the tight
    # retry loop #470 was about.
    exc = nats.errors.Error("nats: 'authorization violation'")
    assert _classify_terminal_nats_error(exc) == "authorization_violation"


def test_classify_transient_returns_none() -> None:
    # These are the cold-start-race cases we DO want to keep retrying —
    # they must not be classified as terminal or connect_nats will bail
    # instantly during a normal maki-nerve-nats boot.
    assert _classify_terminal_nats_error(ConnectionRefusedError()) is None
    assert _classify_terminal_nats_error(OSError("connection refused")) is None
    assert _classify_terminal_nats_error(nats.errors.NoServersError()) is None
    assert _classify_terminal_nats_error(TimeoutError()) is None


def test_classify_unrelated_exception_returns_none() -> None:
    assert _classify_terminal_nats_error(RuntimeError("something else")) is None


# --- NatsTerminalError shape --------------------------------------------------


def test_terminal_error_carries_reason_and_original() -> None:
    original = nats.errors.AuthorizationError()
    err = NatsTerminalError("bad token", reason="AuthorizationError", original=original)
    assert err.reason == "AuthorizationError"
    assert err.original is original
    assert str(err) == "bad token"
    # Subclass of nats.errors.Error so existing ``except nats.errors.Error``
    # handlers still catch it (unlikely — most call sites use bare Exception —
    # but a nats-py 3.x consumer would).
    assert isinstance(err, nats.errors.Error)


# --- connect_nats behaviour ---------------------------------------------------


def test_connect_nats_raises_terminal_on_auth_violation() -> None:
    """Terminal auth failure short-circuits the retry loop.

    Without the #470 fix, ``max_retries=12`` × exponential backoff (capped
    at 30s) meant recall spent ~2min looping on a permanent error before
    giving up — plenty of time for immune to classify it as ``initializing``
    and skip escalation. This test locks in the "raise immediately" path.
    """
    call_count = 0

    async def _fake_connect(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise nats.errors.Error("nats: 'Authorization Violation'")

    async def scenario() -> None:
        # Monkeypatch nats.connect to our fake for the duration of the call.
        import maki_common.nats as mod

        real = mod.nats.connect
        mod.nats.connect = _fake_connect  # type: ignore[assignment]
        try:
            raised: NatsTerminalError | None = None
            try:
                # max_retries=5 to prove we don't burn all 5 attempts on a
                # permanent failure.
                await connect_nats("nats://fake:4222", token="bad", max_retries=5, base_delay=0.001)
            except NatsTerminalError as e:
                raised = e
            assert raised is not None, "expected NatsTerminalError"
            assert call_count == 1, f"expected 1 call (terminal short-circuit), got {call_count}"
            assert raised.reason == "authorization_violation"
            assert "Authorization Violation" in str(raised.original)
        finally:
            mod.nats.connect = real  # type: ignore[assignment]

    _run(scenario())


def test_connect_nats_retries_on_transient() -> None:
    """Cold-start race with maki-nerve-nats still gets its retry budget."""
    call_count = 0

    async def _fake_connect(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise ConnectionRefusedError("nats not up yet")

    async def scenario() -> None:
        import maki_common.nats as mod

        real = mod.nats.connect
        mod.nats.connect = _fake_connect  # type: ignore[assignment]
        try:
            raised: ConnectionRefusedError | None = None
            try:
                await connect_nats("nats://fake:4222", max_retries=3, base_delay=0.001, max_delay=0.001)
            except ConnectionRefusedError as e:
                raised = e
            assert raised is not None, "expected ConnectionRefusedError to propagate"
            # Full retry budget consumed — classifier correctly rejected this as
            # non-terminal.
            assert call_count == 3, f"expected 3 attempts (transient retry), got {call_count}"
        finally:
            mod.nats.connect = real  # type: ignore[assignment]

    _run(scenario())
