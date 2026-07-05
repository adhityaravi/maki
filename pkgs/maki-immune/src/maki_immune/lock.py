"""Async context manager for the infrastructure lock.

Wraps the acquire/release NATS-KV pair behind an ``async with`` so callers can't
forget to release it (issue #127). One site — the deploy propagation handler —
had a success path that wasn't wrapped in ``try/finally`` and depended on the
surrounding handler's ``except`` for release; ``LockNotAcquired`` + ``finally``
inside the CM removes that class of leak permanently.

The CM lives in its own module (rather than ``main.py``) so ``deploy.py`` /
``health.py`` can import :class:`LockNotAcquired` without a circular import back
into ``main`` — and so the primitive is easy to migrate to
``maki_common.kv_lease`` alongside ears' leader election (#114) when that lands.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class LockNotAcquired(Exception):
    """Raised by :func:`infra_lock` when the KV lock is held by another holder.

    Callers catch this to render their site-specific "lock held" response —
    ``msg.respond(...)`` for request handlers, ``_publish_alert(...)`` for
    propagate handlers, a warning log for reflex/stuck-recovery.
    """


@asynccontextmanager
async def infra_lock(
    holder: str,
    *,
    acquire: Callable[..., Awaitable[bool]],
    release: Callable[[str], Awaitable[None]],
    ttl: int = 300,
) -> AsyncIterator[None]:
    """Acquire the site-wide infrastructure KV lock for the duration of the block.

    Raises :class:`LockNotAcquired` if the lock is held elsewhere. Always
    releases on exit — success, exception, or cancellation. ``acquire`` /
    ``release`` are injected so this module has no dependency on the
    ``_lock_kv`` module-global that lives in :mod:`maki_immune.main`.
    """
    if not await acquire(holder, ttl=ttl):
        raise LockNotAcquired(holder)
    try:
        yield
    finally:
        await release(holder)
