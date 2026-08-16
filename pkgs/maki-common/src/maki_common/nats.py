"""NATS connection and KV bucket utilities."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import nats
import nats.errors
import nats.js.errors
from nats.aio.client import Client
from nats.js.kv import KeyValue

log = logging.getLogger(__name__)


# Server-sent -ERR strings that indicate a permanent, retry-immune failure.
# NATS surfaces these as ``nats.errors.Error("nats: '<message>'")`` from the
# protocol handler — a bare ``Error`` with the message baked into ``str(exc)``
# rather than a distinct subclass — so message matching is unavoidable. See
# nats-py's ``client.py`` ``_process_err`` and issue #470: a broken NATS
# token had ``connect_nats`` retrying an ``Authorization Violation`` in a
# tight backoff loop forever, treating a config mismatch the same as a
# cold-start race with maki-nerve-nats.
_TERMINAL_NATS_MESSAGES: tuple[str, ...] = (
    "Authorization Violation",
    "Authorization Timeout",
    "TLS Required",
    "TLS Handshake",
    "Authentication Expired",
    "User Authentication Expired",
    "Invalid Client Protocol",
    "Invalid Connect Config",
    "Invalid Signature",
    "No Credentials",
    "Authentication Timeout",
)

# Client-side exception classes that indicate a permanent failure. A retry
# loop cannot fix a rejected token, a missing TLS cert, or an unparseable
# credentials file — the config is wrong, not the network.
_TERMINAL_NATS_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    nats.errors.AuthorizationError,
    nats.errors.InvalidUserCredentialsError,
    nats.errors.SecureConnRequiredError,
    nats.errors.SecureConnWantedError,
    nats.errors.SecureConnFailedError,
)


class NatsTerminalError(nats.errors.Error):
    """Raised by :func:`connect_nats` on a permanent, retry-immune failure.

    Distinguishes "the config is wrong, human/config change required" from
    "the server is briefly down, keep waiting". Callers (recall's lifespan,
    immune's Claude escalation, etc.) can catch this to surface the terminal
    state on ``/health`` and skip the retry backoff. See issue #470.

    Attributes:
        reason: Short slug (e.g. ``"authorization_violation"``,
            ``"AuthorizationError"``) that observers can key off without
            parsing the message string.
        original: The underlying exception raised by ``nats.connect``.
    """

    def __init__(self, message: str, *, reason: str, original: BaseException) -> None:
        super().__init__(message)
        self.reason = reason
        self.original = original


def _classify_terminal_nats_error(exc: BaseException) -> str | None:
    """Return a short reason slug if ``exc`` is a terminal NATS error, else None.

    Checks exception type first (cheapest, unambiguous) then falls back to
    message matching for the ``nats.errors.Error("nats: '<server-msg>'")``
    shape the protocol handler emits on server -ERR frames.
    """
    if isinstance(exc, _TERMINAL_NATS_EXCEPTION_TYPES):
        return type(exc).__name__
    msg = str(exc).lower()
    for pattern in _TERMINAL_NATS_MESSAGES:
        if pattern.lower() in msg:
            return pattern.lower().replace(" ", "_")
    return None


async def connect_nats(
    url: str,
    token: str | None = None,
    max_retries: int = 12,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Client:
    """Connect to NATS with exponential backoff retry.

    Retries up to max_retries times before giving up. Delay doubles each
    attempt (capped at max_delay), giving roughly 2 minutes of patience for
    NATS to become available — enough to survive cold-start races where the
    pod comes up before maki-nerve-nats is ready.

    Terminal failures (bad auth token, TLS required, invalid credentials
    file) skip the backoff entirely and raise :class:`NatsTerminalError`
    immediately — no amount of retry fixes a config mismatch, and burning
    ``max_retries`` seconds of backoff before surfacing the same permanent
    failure was hiding the real problem behind what looked like a slow
    init (see #470). Callers should catch ``NatsTerminalError`` distinctly
    from other exceptions so ``/health`` can surface the terminal reason
    and immune can escalate to Claude instead of reflex-restarting.

    Args:
        url: NATS server URL.
        token: Optional auth token.
        max_retries: Maximum number of connection attempts (default 12).
        base_delay: Initial retry delay in seconds (default 1.0).
        max_delay: Maximum retry delay in seconds (default 30.0).
    """
    kwargs: dict[str, Any] = {}
    if token:
        kwargs["token"] = token

    delay = base_delay
    for attempt in range(1, max_retries + 1):
        try:
            nc = await nats.connect(url, **kwargs)
            log.info("Connected to NATS", extra={"nats_url": url, "auth": bool(token), "attempt": attempt})
            return nc
        except Exception as exc:
            reason = _classify_terminal_nats_error(exc)
            if reason is not None:
                # Permanent failure — a broken token / missing TLS cert /
                # unparseable creds is not going to fix itself in 2 minutes
                # of backoff. Bail immediately so the caller can decide
                # what to expose on /health. See #470.
                log.error(
                    "NATS connection failed with terminal error — not retrying",
                    extra={
                        "nats_url": url,
                        "attempt": attempt,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "terminal": True,
                        "reason": reason,
                    },
                )
                raise NatsTerminalError(
                    f"terminal NATS error ({reason}): {exc}",
                    reason=reason,
                    original=exc,
                ) from exc
            if attempt >= max_retries:
                log.error(
                    "Failed to connect to NATS after all retries",
                    extra={"nats_url": url, "attempts": attempt},
                )
                raise
            log.warning(
                "NATS connection failed, retrying",
                extra={
                    "nats_url": url,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "retry_in": delay,
                    "error": str(exc),
                },
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    raise RuntimeError("unreachable")  # pragma: no cover


async def init_kv(js, bucket: str, defaults: dict[str, Any] | None = None) -> KeyValue:
    """Create or connect to a KV bucket, optionally seeding defaults.

    Args:
        js: JetStream context.
        bucket: Bucket name.
        defaults: If provided, seed these key/value pairs if they don't exist.
            Values are JSON-encoded before storing.
    """
    try:
        kv = await js.key_value(bucket)
    except Exception:
        kv = await js.create_key_value(bucket=bucket)
        log.info("Created KV bucket", extra={"bucket": bucket})

    if defaults:
        for key, value in defaults.items():
            try:
                await kv.get(key)
            except Exception:
                await kv.put(key, json.dumps(value).encode())
                log.info("Seeded KV default", extra={"bucket": bucket, "key": key, "value": value})

    return kv


async def load_kv_config(kv: KeyValue, defaults: dict[str, Any]) -> dict[str, Any]:
    """Load config from a KV bucket, falling back to provided defaults."""
    config = {}
    for key, default in defaults.items():
        try:
            entry = await kv.get(key)
            config[key] = json.loads(entry.value.decode())
        except Exception:
            config[key] = default
    return config


async def kv_acquire_lease(
    kv: KeyValue,
    key: str,
    ttl: float,
    instance_id: str,
    *,
    allow_renew: bool = False,
) -> bool:
    """Acquire a TTL-bounded lease via NATS KV optimistic CAS.

    A single primitive that covers two patterns:

    * One-shot loop claim (``allow_renew=False``): used to gate periodic work
      across replicas. The current holder cannot re-claim within ``ttl``
      seconds — they must wait for the claim to expire. Used by
      :func:`try_claim_loop`.
    * Renewable lease (``allow_renew=True``): used for singleton leader
      election. The current holder refreshes their own claim before it
      expires; another instance can only take over after expiry.

    Args:
        kv: KV bucket (e.g. maki-lock).
        key: Lease key (e.g. "ears.leader", "loop.stem.idle").
        ttl: Lease duration in seconds.
        instance_id: Unique ID for this process instance.
        allow_renew: If True and we're the current holder of a fresh claim,
            renew it via CAS. If False, return False without renewing.

    Returns:
        True if this instance now holds the lease, False otherwise.
    """
    now = time.time()
    claim = json.dumps({"instance": instance_id, "claimed_at": now}).encode()

    try:
        entry = await kv.get(key)
        data = json.loads(entry.value.decode())
        if now - data.get("claimed_at", 0) < ttl:
            if allow_renew and data.get("instance") == instance_id:
                # We're already the holder — renew the lease via CAS
                try:
                    await kv.update(key, claim, entry.revision)
                    return True
                except Exception:
                    return False
            return False
        # Lease expired — try to take over via CAS
        try:
            await kv.update(key, claim, entry.revision)
            return True
        except Exception:
            return False
    except nats.js.errors.KeyNotFoundError:
        try:
            await kv.create(key, claim)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def try_claim_loop(kv: KeyValue, key: str, interval: float, instance_id: str) -> bool:
    """Try to claim a periodic loop iteration via NATS KV CAS.

    Thin wrapper around :func:`kv_acquire_lease` with ``allow_renew=False``.
    Prevents multiple instances from running the same timed loop concurrently
    — first instance to update wins, others skip.

    Args:
        kv: KV bucket (e.g. maki-lock).
        key: Claim key (e.g. "loop.stem.idle").
        interval: Minimum seconds between claims.
        instance_id: Unique ID for this process instance.

    Returns:
        True if this instance should run, False if another claimed it.
    """
    return await kv_acquire_lease(kv, key, interval, instance_id, allow_renew=False)


async def kv_put_float(kv: KeyValue, key: str, value: float) -> None:
    """Store a float in NATS KV."""
    await kv.put(key, json.dumps(value).encode())


async def kv_get_float(kv: KeyValue, key: str, default: float = 0.0) -> float:
    """Read a float from NATS KV."""
    try:
        entry = await kv.get(key)
        return json.loads(entry.value.decode())
    except Exception:
        return default


# --- Supervised subscriptions ---

# Sentinel returned by ``subscribe_supervised`` so callers don't accidentally
# treat the helper as returning a value. The function is meant to run forever;
# it only exits via ``CancelledError``.
MessageHandler = Callable[[Any], Awaitable[None]]


async def subscribe_supervised(
    nc: Any,
    subject: str,
    handler: MessageHandler,
    *,
    queue: str | None = None,
    js: Any = None,
    durable: str | None = None,
    deliver_policy: Any = None,
    auto_ack: bool | None = None,
    ack_on_error: bool = False,
    nak_delay: float | None = None,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    name: str | None = None,
) -> None:
    """Long-running supervised NATS subscription loop.

    Subscribes to ``subject`` and dispatches every incoming message to
    ``handler``. If the underlying ``async for sub.messages`` generator returns
    — subscription unsubscribed, NATS connection terminally closed, internal
    queue drain — this helper logs at WARNING level and re-subscribes with
    exponential backoff. Same for failures inside ``await nc.subscribe`` /
    ``js.subscribe`` itself.

    Without this wrapper a bare ``async for msg in sub.messages`` loop exits
    silently when the iterator is exhausted: the asyncio task simply
    ``Task: result=None``s and the service goes blind to that subject with no
    log, no health flip, no restart. See issue #175.

    Ack semantics (issue #221):

    * Handler success → ``await msg.ack()`` when ``auto_ack`` is truthy. The
      message is consumed and JetStream advances the consumer cursor.
    * Handler exception → ``await msg.nak(delay=nak_delay)`` by default, so
      JetStream redelivers per the consumer's ``max_deliver`` / ``ack_wait``
      policy. This is the at-least-once contract durable consumers rely on:
      a transient kubectl/Postgres/Discord hiccup must not silently consume
      the message.
    * Handler exception with ``ack_on_error=True`` → ``await msg.ack()``
      anyway. Opt-in for fire-and-forget broadcasts where dropping a poison
      message is better than redelivering forever.
    * Core NATS (no ``js``) has no NAK concept — exceptions are logged and
      the message moves on regardless.

    Args:
        nc: Core NATS client (used for non-JetStream subscriptions and as a
            connection-state probe).
        subject: NATS subject to subscribe to.
        handler: Async callable receiving each message.
        queue: Optional queue group for core NATS subscribe.
        js: JetStream context. When provided, ``js.subscribe`` is used and the
            ``durable`` / ``deliver_policy`` kwargs apply.
        durable: JetStream durable consumer name (only with ``js``).
        deliver_policy: JetStream deliver policy (only with ``js``).
        auto_ack: If True, the supervisor ACKs on success and NAKs on
            failure (subject to ``ack_on_error``). Default: True for
            JetStream subs, False for core NATS subs.
        ack_on_error: If True, ACK the message even when the handler raises
            (legacy fire-and-forget behavior — exception is logged and the
            message is permanently consumed). If False (default), the
            message is NAK'd so JetStream redelivers per the consumer's
            ``max_deliver`` policy. Set this only for non-durable broadcasts
            where silently losing a message on transient errors is preferable
            to indefinite redelivery.
        nak_delay: Optional seconds to delay redelivery on NAK. If None,
            JetStream uses the consumer's backoff / ``ack_wait`` default.
        base_delay: Initial subscribe-retry backoff delay in seconds (default
            1.0). Unrelated to ``nak_delay`` — this governs the supervisor's
            re-subscribe loop, not per-message redelivery.
        max_delay: Maximum subscribe-retry backoff delay in seconds (default
            30.0).
        name: Optional human-readable label for logs (defaults to ``subject``).

    The coroutine never returns under normal operation — it loops forever and
    only exits via ``asyncio.CancelledError``.
    """
    label = name or subject
    if auto_ack is None:
        auto_ack = js is not None
    delay = base_delay

    while True:
        try:
            if js is not None:
                kwargs: dict[str, Any] = {}
                if durable is not None:
                    kwargs["durable"] = durable
                if deliver_policy is not None:
                    kwargs["deliver_policy"] = deliver_policy
                sub = await js.subscribe(subject, **kwargs)
            elif queue is not None:
                sub = await nc.subscribe(subject, queue=queue)
            else:
                sub = await nc.subscribe(subject)

            log.info(
                "Supervised subscription active",
                extra={"subject": subject, "sub_name": label, "jetstream": js is not None, "durable": durable},
            )
            delay = base_delay  # reset backoff after a successful subscribe

            async for msg in sub.messages:
                handler_failed = False
                try:
                    await handler(msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    handler_failed = True
                    log.exception(
                        "Supervised handler error",
                        extra={"subject": subject, "sub_name": label},
                    )

                if not auto_ack:
                    continue

                # Settle the message. Success → ACK. Failure → NAK so JS
                # redelivers (unless caller opted into ack_on_error). Core
                # NATS messages lack ``nak`` entirely; in that case the
                # failure is already logged and we just move on.
                try:
                    if handler_failed and not ack_on_error:
                        nak = getattr(msg, "nak", None)
                        if nak is None:
                            # Core NATS: no redelivery primitive. Already
                            # logged above; nothing else to do.
                            continue
                        if nak_delay is not None:
                            await nak(delay=nak_delay)
                        else:
                            await nak()
                    else:
                        await msg.ack()
                except Exception:
                    log.exception(
                        "Supervised ack/nak failed",
                        extra={
                            "subject": subject,
                            "sub_name": label,
                            "handler_failed": handler_failed,
                            "ack_on_error": ack_on_error,
                        },
                    )

            # async for exited without raising — subscription drained or the
            # NATS client closed it. Loop and re-subscribe; this is the whole
            # point of the supervisor.
            log.warning(
                "Supervised subscription stream ended — re-subscribing",
                extra={"subject": subject, "sub_name": label, "retry_in": delay},
            )
        except asyncio.CancelledError:
            log.info("Supervised subscription cancelled", extra={"subject": subject, "sub_name": label})
            raise
        except Exception:
            log.exception(
                "Supervised subscribe failed — retrying",
                extra={"subject": subject, "sub_name": label, "retry_in": delay},
            )

        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)
