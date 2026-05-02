"""Tests for maki_common.repo — token redaction and the RepoRegistry."""

from __future__ import annotations

import asyncio
from pathlib import Path

from maki_common.repo import RepoEntry, RepoRegistry, redact_token


def test_redact_token_strips_url_form() -> None:
    """The exact form git echoes on a 403 must lose the secret."""
    msg = (
        "fatal: unable to access "
        "'https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz1234567890@github.com/owner/repo.git/': "
        "The requested URL returned error: 403"
    )
    redacted = redact_token(msg)
    assert "ghs_" not in redacted
    assert "x-access-token:***@" in redacted
    assert "github.com/owner/repo.git" in redacted


def test_redact_token_strips_bare_token() -> None:
    """Defensive: bare ghs_ / ghp_ tokens get stripped even outside a URL."""
    for prefix in ("ghs_", "ghp_", "gho_", "ghu_", "ghr_"):
        token = f"{prefix}abcdefghijklmnopqrstuvwxyz1234567890"
        msg = f"token leaked: {token} oops"
        redacted = redact_token(msg)
        assert token not in redacted
        assert "***" in redacted


def test_redact_token_handles_multiple_occurrences() -> None:
    msg = (
        "https://x-access-token:ghs_AAAAAAAAAAAAAAAAAAAA@github.com/a/b "
        "and again https://x-access-token:ghs_BBBBBBBBBBBBBBBBBBBB@github.com/c/d"
    )
    redacted = redact_token(msg)
    assert "ghs_AAAA" not in redacted
    assert "ghs_BBBB" not in redacted
    assert redacted.count("x-access-token:***@") == 2


def test_redact_token_passthrough_safe_text() -> None:
    """No tokens → text is returned as-is, no spurious mangling."""
    safe = "Pull failed: refusing to merge unrelated histories"
    assert redact_token(safe) == safe


def test_redact_token_handles_empty_and_none_like() -> None:
    assert redact_token("") == ""


# ---------------------------------------------------------------------------
# RepoRegistry — multi-repo workspace resolution.
#
# Use plain `asyncio.run` so the suite stays free of pytest-asyncio.
# ---------------------------------------------------------------------------


def _make_git_dir(tmp_path: Path, name: str) -> Path:
    """Create a fake repo workspace with a `.git` dir so the resolver skips clone."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def test_registry_resolves_default_when_repo_arg_missing(tmp_path: Path) -> None:
    repo = _make_git_dir(tmp_path, "maki")
    reg = RepoRegistry(workspace_root=str(tmp_path))
    reg.register(RepoEntry(path=str(repo), owner="adhityaravi", name="maki"), default=True)

    entry = asyncio.run(reg.resolve(None))
    assert entry is not None
    assert entry.path == str(repo)
    assert entry.name == "maki"

    entry = asyncio.run(reg.resolve(""))
    assert entry is not None
    assert entry.name == "maki"


def test_registry_resolves_known_short_and_full_keys(tmp_path: Path) -> None:
    repo = _make_git_dir(tmp_path, "maki")
    reg = RepoRegistry(workspace_root=str(tmp_path))
    reg.register(RepoEntry(path=str(repo), owner="adhityaravi", name="maki"), default=True)

    by_name = asyncio.run(reg.resolve("maki"))
    by_full = asyncio.run(reg.resolve("adhityaravi/maki"))
    assert by_name is by_full


def test_registry_returns_none_for_unknown_short_name(tmp_path: Path) -> None:
    repo = _make_git_dir(tmp_path, "maki")
    reg = RepoRegistry(workspace_root=str(tmp_path))
    reg.register(RepoEntry(path=str(repo), owner="adhityaravi", name="maki"), default=True)

    assert asyncio.run(reg.resolve("charmarr")) is None


def test_registry_returns_none_when_no_default_and_no_arg() -> None:
    reg = RepoRegistry(workspace_root="/tmp")
    assert asyncio.run(reg.resolve(None)) is None
    assert reg.default() is None


def test_registry_auto_registers_owner_slash_name_for_existing_clone(tmp_path: Path) -> None:
    """If the workspace already has a `.git` dir, no clone is attempted."""
    repo = _make_git_dir(tmp_path, "charmarr")
    reg = RepoRegistry(workspace_root=str(tmp_path))
    # No default, but owner/name still resolves because the path already exists.
    entry = asyncio.run(reg.resolve("adhityaravi/charmarr"))
    assert entry is not None
    assert entry.owner == "adhityaravi"
    assert entry.name == "charmarr"
    assert entry.path == str(repo)
    # Now it's known by both short and full names.
    assert "charmarr" in reg.known()
    assert "adhityaravi/charmarr" in reg.known()


def test_registry_inherits_default_auth_for_auto_registered(tmp_path: Path) -> None:
    _make_git_dir(tmp_path, "maki")
    _make_git_dir(tmp_path, "charmarr")
    sentinel_auth = object()
    reg = RepoRegistry(workspace_root=str(tmp_path))
    reg.register(
        RepoEntry(
            path=str(tmp_path / "maki"),
            owner="adhityaravi",
            name="maki",
            auth=sentinel_auth,
        ),
        default=True,
    )

    entry = asyncio.run(reg.resolve("adhityaravi/charmarr"))
    assert entry is not None
    assert entry.auth is sentinel_auth


def test_registry_rejects_malformed_owner_slash_name() -> None:
    reg = RepoRegistry(workspace_root="/tmp")
    assert asyncio.run(reg.resolve("/")) is None
    assert asyncio.run(reg.resolve("owner/")) is None
    assert asyncio.run(reg.resolve("/name")) is None


def test_repo_entry_default_clone_url_uses_owner_name() -> None:
    entry = RepoEntry(path="/repo/charmarr", owner="adhityaravi", name="charmarr")
    assert entry.resolved_clone_url() == "https://github.com/adhityaravi/charmarr.git"


def test_repo_entry_explicit_clone_url_overrides() -> None:
    entry = RepoEntry(
        path="/repo/x",
        owner="o",
        name="x",
        clone_url="https://example.invalid/x.git",
    )
    assert entry.resolved_clone_url() == "https://example.invalid/x.git"
