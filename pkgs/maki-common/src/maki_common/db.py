"""PostgreSQL DSN construction.

Single source of truth for the Postgres connection string used by every
Maki service that talks to maki-vault. Previously stem and recall each
built their own DSN and the two drifted — stem string-interpolated the
``POSTGRES_HOST`` env var straight into the URL, which produces an invalid
DSN the moment vault is configured for HA (``POSTGRES_HOST`` set to a
comma-separated list). See issue #130.

This helper:

* URL-encodes ``POSTGRES_USER`` and ``POSTGRES_PASSWORD``.
* Parses ``POSTGRES_HOST`` as a comma-separated host list and emits the
  libpq multi-host netloc (``host-0:5432,host-1:5432``). Single-host is
  just the degenerate one-item case.
* Appends ``target_session_attrs=read-write`` by default so libpq (and
  asyncpg's libpq-compatible URI parser) walk the host list and pick the
  writable primary during a vault failover.
* Optionally appends ``connect_timeout`` so callers that want a fast
  boot-time fail-fast (recall's #312 mitigation) get it in the URI
  itself.

asyncpg compatibility: asyncpg parses libpq-style multi-host URIs and
honours ``target_session_attrs`` in the query string (added upstream in
0.28 — well below the version pinned in the fleet). If we ever need to
target an older client that rejects the parameter, callers can pass
``target_session_attrs=None`` to omit it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote_plus


def build_pg_dsn(
    *,
    target_session_attrs: str | None = "read-write",
    connect_timeout: int | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Build a libpq-compatible PostgreSQL DSN from environment variables.

    Reads (with defaults): ``POSTGRES_USER`` ("maki"),
    ``POSTGRES_PASSWORD`` (""), ``POSTGRES_HOST`` ("maki-vault",
    comma-separated for HA), ``POSTGRES_PORT`` ("5432"), ``POSTGRES_DB``
    ("maki").

    Args:
        target_session_attrs: libpq session-attribute filter. Defaults to
            ``"read-write"`` so multi-host DSNs prefer the writable
            primary. Pass ``None`` to omit the parameter entirely (e.g.
            for a read-only replica connection).
        connect_timeout: libpq ``connect_timeout`` in seconds. ``None``
            (default) omits the parameter.
        env: Environment mapping to read from. Defaults to
            ``os.environ``; override in tests.

    Returns:
        A ``postgresql://user:pass@host[,host]:port/db?...`` URI.
    """
    e = env if env is not None else os.environ

    user = quote_plus(e.get("POSTGRES_USER", "maki"))
    raw_password = e.get("POSTGRES_PASSWORD", "")
    password = quote_plus(raw_password)
    hosts = e.get("POSTGRES_HOST", "maki-vault")
    port = e.get("POSTGRES_PORT", "5432")
    db = e.get("POSTGRES_DB", "maki")

    host_port = ",".join(f"{h.strip()}:{port}" for h in hosts.split(",") if h.strip())

    params: list[str] = []
    if target_session_attrs:
        params.append(f"target_session_attrs={target_session_attrs}")
    if connect_timeout is not None:
        params.append(f"connect_timeout={connect_timeout}")
    query = f"?{'&'.join(params)}" if params else ""

    return f"postgresql://{user}:{password}@{host_port}/{db}{query}"


__all__ = ["build_pg_dsn"]
