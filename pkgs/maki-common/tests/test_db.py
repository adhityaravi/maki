"""Tests for ``maki_common.db.build_pg_dsn``.

These are the regression tests for issue #130 — the fleet had two Postgres
DSN builders that drifted; stem's single-host builder would emit an
invalid URI the moment vault ran as an HA pair. Every path that used to
diverge between stem and recall is pinned here.
"""

from __future__ import annotations

from maki_common.db import build_pg_dsn


def test_single_host_default() -> None:
    dsn = build_pg_dsn(
        env={
            "POSTGRES_USER": "maki",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_HOST": "maki-vault",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "maki",
        }
    )
    assert dsn == "postgresql://maki:secret@maki-vault:5432/maki?target_session_attrs=read-write"


def test_multi_host_ha_failover() -> None:
    # The exact case that broke stem in #130: HA vault behind two pods.
    dsn = build_pg_dsn(
        env={
            "POSTGRES_USER": "maki",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_HOST": "maki-vault-0.maki-vault,maki-vault-1.maki-vault",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "maki",
        }
    )
    assert dsn == (
        "postgresql://maki:secret@"
        "maki-vault-0.maki-vault:5432,maki-vault-1.maki-vault:5432/maki"
        "?target_session_attrs=read-write"
    )


def test_multi_host_strips_whitespace() -> None:
    # POSTGRES_HOST set via a heredoc / YAML block scalar can pick up
    # incidental whitespace between commas. Don't emit " host:5432" — libpq
    # treats leading whitespace as part of the hostname.
    dsn = build_pg_dsn(
        env={
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_HOST": "host-0 , host-1 ,host-2",
        }
    )
    assert "host-0:5432,host-1:5432,host-2:5432" in dsn


def test_missing_password_still_valid_uri() -> None:
    # Stem's original builder tolerated an empty password (default "")
    # and emitted ``postgresql://maki:@host/db``. Keep that behavior —
    # recall's KeyError-on-missing was the outlier, not the contract.
    dsn = build_pg_dsn(env={"POSTGRES_HOST": "maki-vault"})
    assert dsn == "postgresql://maki:@maki-vault:5432/maki?target_session_attrs=read-write"


def test_url_encodes_user_and_password() -> None:
    # Special characters in the password would otherwise break URI parsing
    # (``@`` in a password would confuse the netloc split, ``/`` would
    # look like the start of the path).
    dsn = build_pg_dsn(
        env={
            "POSTGRES_USER": "user@corp",
            "POSTGRES_PASSWORD": "p@ss/word:hi",
            "POSTGRES_HOST": "maki-vault",
        }
    )
    assert "user%40corp" in dsn
    assert "p%40ss%2Fword%3Ahi" in dsn


def test_target_session_attrs_can_be_disabled() -> None:
    # A read-only replica caller (or an asyncpg version too old to
    # honour the param) can opt out.
    dsn = build_pg_dsn(
        target_session_attrs=None,
        env={"POSTGRES_PASSWORD": "s", "POSTGRES_HOST": "maki-vault"},
    )
    assert "target_session_attrs" not in dsn
    assert dsn == "postgresql://maki:s@maki-vault:5432/maki"


def test_target_session_attrs_can_be_overridden() -> None:
    dsn = build_pg_dsn(
        target_session_attrs="any",
        env={"POSTGRES_PASSWORD": "s", "POSTGRES_HOST": "maki-vault"},
    )
    assert "target_session_attrs=any" in dsn


def test_connect_timeout_appended_when_set() -> None:
    dsn = build_pg_dsn(
        connect_timeout=10,
        env={"POSTGRES_PASSWORD": "s", "POSTGRES_HOST": "maki-vault"},
    )
    assert dsn.endswith("?target_session_attrs=read-write&connect_timeout=10")


def test_connect_timeout_omitted_by_default() -> None:
    dsn = build_pg_dsn(env={"POSTGRES_PASSWORD": "s", "POSTGRES_HOST": "maki-vault"})
    assert "connect_timeout" not in dsn


def test_defaults_when_env_empty() -> None:
    dsn = build_pg_dsn(env={})
    assert dsn == "postgresql://maki:@maki-vault:5432/maki?target_session_attrs=read-write"


def test_custom_port_applied_to_every_host() -> None:
    dsn = build_pg_dsn(
        env={
            "POSTGRES_PASSWORD": "s",
            "POSTGRES_HOST": "a,b,c",
            "POSTGRES_PORT": "6543",
        }
    )
    assert "a:6543,b:6543,c:6543" in dsn
