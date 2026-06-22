"""Tests for maki_common.repo — token redaction and the RepoRegistry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

from maki_common.repo import RepoEntry, RepoRegistry, SyncError, hard_sync, redact_token


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


def test_redact_token_strips_basic_auth_header() -> None:
    """Issue #347: tokens now ride in Authorization: Basic <b64> headers.

    The header value is base64-encoded so the bare-token regex won't catch
    it; the dedicated Basic-auth pattern must.
    """
    import base64

    encoded = base64.b64encode(b"x-access-token:ghs_aaaaaaaaaaaaaaaaaaaa").decode()
    msg = f"some diagnostic: Authorization: Basic {encoded} (do not log this)"
    redacted = redact_token(msg)
    assert encoded not in redacted
    assert "Authorization: Basic ***" in redacted


def test_hard_sync_never_writes_token_into_remote_url() -> None:
    """Regression for issue #347.

    Older versions ran `git remote set-url origin https://x-access-token:TOKEN@...`
    which persisted the installation token on disk in `.git/config` for the
    full ~1h token TTL — readable by any process with filesystem access. The
    fix injects auth per-invocation via `-c http.extraheader=...` instead.

    This test pins the new invariant: the value passed to `set-url` must
    NEVER contain the token. Catches any future refactor that re-introduces
    the embedded-token URL pattern.
    """
    secret_token = "ghs_supersecret_NEVER_ON_DISK_xxxxxxxxxx"

    class _LeakingAuth:
        async def get_token(self) -> str:
            return secret_token

    stub, calls = _fake_run_git(
        (0, "", ""),  # remote set-url
        (0, "", ""),  # fetch
        (0, "", ""),  # reset
        (0, "", ""),  # clean
    )
    with mock.patch("maki_common.repo._run_git", stub):
        asyncio.run(
            hard_sync(
                "/repo/maki",
                github_auth=_LeakingAuth(),
                owner="adhityaravi",
                name="maki",
            )
        )

    # The secret must NEVER appear in any positional arg passed to git.
    # `set-url` writes its argument to `.git/config`; if the token shows up
    # here, it's on disk.
    for argv in calls:
        for arg in argv:
            assert secret_token not in arg, (
                f"Issue #347 regression: installation token leaked into git argv {argv!r}; "
                "tokens must be injected via `_run_git(..., token=...)` only."
            )


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


# ---------------------------------------------------------------------------
# hard_sync — the abort-on-first-failure replacement for cortex's inline
# fetch/reset/clean pipeline. The original bug (#290) was that a failed
# `git fetch` only logged a warning and the loop kept going to
# `git reset --hard origin/main`, silently running the turn against stale
# code. These tests pin down the new "raise SyncError, do nothing more"
# contract by stubbing out `_run_git` and asserting both the call order and
# the failure semantics.
# ---------------------------------------------------------------------------


def _fake_run_git(*results: tuple[int, str, str]):
    """Return an async stub for `_run_git` that yields `results` in order.

    Also records every call so tests can assert that hard_sync stops at the
    first non-zero step (no `reset --hard` after a failed `fetch`).

    The stub accepts a `token=` kwarg (issue #347 — installation tokens are
    now injected per-invocation via `_run_git(..., token=...)`) and exposes
    the per-call token under ``stub.token_calls`` so tests can verify that
    fetch/pull/push run with auth and local-only steps (reset, clean) do not.
    """
    calls: list[tuple[str, ...]] = []
    token_calls: list[str | None] = []
    iterator = iter(results)

    async def stub(repo_path: str, *args: str, token: str | None = None) -> tuple[int, str, str]:
        calls.append(args)
        token_calls.append(token)
        try:
            return next(iterator)
        except StopIteration:  # pragma: no cover — defensive
            raise AssertionError(f"_run_git called more times than stubbed: {args}") from None

    stub.token_calls = token_calls  # type: ignore[attr-defined]
    return stub, calls


def test_hard_sync_success_runs_fetch_reset_clean_in_order() -> None:
    stub, calls = _fake_run_git(
        (0, "", ""),  # fetch
        (0, "", ""),  # reset
        (0, "", ""),  # clean
    )
    with mock.patch("maki_common.repo._run_git", stub):
        asyncio.run(hard_sync("/repo/maki"))
    assert calls == [
        ("fetch", "origin", "main"),
        ("reset", "--hard", "origin/main"),
        ("clean", "-fd"),
    ]


def test_hard_sync_aborts_on_fetch_failure_does_not_reset() -> None:
    """The whole point: a failed fetch must NOT proceed to reset/clean."""
    stub, calls = _fake_run_git(
        (1, "", "fatal: unable to access remote: connection refused"),
    )
    with mock.patch("maki_common.repo._run_git", stub):
        try:
            asyncio.run(hard_sync("/repo/maki"))
        except SyncError as exc:
            assert exc.step == "fetch"
            assert exc.returncode == 1
            assert "connection refused" in exc.stderr
        else:  # pragma: no cover — should have raised
            raise AssertionError("hard_sync should have raised SyncError on fetch failure")
    # Critical assertion for issue #290: no reset --hard ran after the failed fetch.
    assert calls == [("fetch", "origin", "main")]


def test_hard_sync_aborts_on_reset_failure_does_not_clean() -> None:
    stub, calls = _fake_run_git(
        (0, "", ""),  # fetch OK
        (128, "", "fatal: reset failed"),  # reset fails
    )
    with mock.patch("maki_common.repo._run_git", stub):
        try:
            asyncio.run(hard_sync("/repo/maki"))
        except SyncError as exc:
            assert exc.step == "reset"
        else:  # pragma: no cover
            raise AssertionError("hard_sync should have raised on reset failure")
    assert calls == [
        ("fetch", "origin", "main"),
        ("reset", "--hard", "origin/main"),
    ]


def test_hard_sync_aborts_on_clean_failure() -> None:
    stub, calls = _fake_run_git(
        (0, "", ""),
        (0, "", ""),
        (1, "", "could not unlink working tree file"),
    )
    with mock.patch("maki_common.repo._run_git", stub):
        try:
            asyncio.run(hard_sync("/repo/maki"))
        except SyncError as exc:
            assert exc.step == "clean"
            assert "unlink" in exc.stderr
        else:  # pragma: no cover
            raise AssertionError("hard_sync should have raised on clean failure")
    assert len(calls) == 3


class _FakeAuth:
    def __init__(self, token: str = "ghs_test_token_xxxxxxxxxxxxxxxxxxxx") -> None:
        self._token = token

    async def get_token(self) -> str:
        return self._token


def test_hard_sync_with_auth_sets_remote_url_first_then_fetch() -> None:
    stub, calls = _fake_run_git(
        (0, "", ""),  # remote set-url
        (0, "", ""),  # fetch
        (0, "", ""),  # reset
        (0, "", ""),  # clean
    )
    with mock.patch("maki_common.repo._run_git", stub):
        asyncio.run(
            hard_sync(
                "/repo/maki",
                github_auth=_FakeAuth(),
                owner="adhityaravi",
                name="maki",
            )
        )
    assert calls[0][:3] == ("remote", "set-url", "origin")
    # Issue #347: the URL written to .git/config must be token-free. The
    # installation token is injected per-invocation on the fetch step below
    # via `git -c http.extraheader=...` instead.
    assert "x-access-token:" not in calls[0][3]
    assert "ghs_" not in calls[0][3]
    assert calls[0][3] == "https://github.com/adhityaravi/maki.git"
    assert calls[1:] == [
        ("fetch", "origin", "main"),
        ("reset", "--hard", "origin/main"),
        ("clean", "-fd"),
    ]
    # And the token flows only to the network-facing step. set-url, reset,
    # clean are all local and must run without auth.
    assert stub.token_calls[0] is None  # type: ignore[attr-defined]
    assert stub.token_calls[1] == "ghs_test_token_xxxxxxxxxxxxxxxxxxxx"  # type: ignore[attr-defined]
    assert stub.token_calls[2] is None  # type: ignore[attr-defined]
    assert stub.token_calls[3] is None  # type: ignore[attr-defined]


def test_hard_sync_aborts_on_set_url_failure_does_not_fetch() -> None:
    """A silent set-url failure was a sub-bug of #290 — make it loud."""
    stub, calls = _fake_run_git(
        (1, "", "fatal: bad remote"),  # set-url fails
    )
    with mock.patch("maki_common.repo._run_git", stub):
        try:
            asyncio.run(
                hard_sync(
                    "/repo/maki",
                    github_auth=_FakeAuth(),
                    owner="adhityaravi",
                    name="maki",
                )
            )
        except SyncError as exc:
            assert exc.step == "remote set-url"
        else:  # pragma: no cover
            raise AssertionError("hard_sync should have raised on set-url failure")
    # Critical: no fetch ran after the failed set-url.
    assert calls == [("remote", "set-url", "origin", calls[0][3])]


def test_hard_sync_with_auth_requires_url_or_owner_name() -> None:
    try:
        asyncio.run(hard_sync("/repo/maki", github_auth=_FakeAuth()))
    except ValueError as exc:
        assert "clone_url" in str(exc) or "owner" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("hard_sync should require owner/name or clone_url with github_auth")


def test_hard_sync_token_mint_failure_surfaces_as_sync_error() -> None:
    class _BrokenAuth:
        async def get_token(self) -> str:
            raise RuntimeError("installation revoked")

    stub, calls = _fake_run_git()  # no git calls expected
    with mock.patch("maki_common.repo._run_git", stub):
        try:
            asyncio.run(
                hard_sync(
                    "/repo/maki",
                    github_auth=_BrokenAuth(),
                    owner="adhityaravi",
                    name="maki",
                )
            )
        except SyncError as exc:
            assert exc.step == "token"
            assert "installation revoked" in exc.stderr
        else:  # pragma: no cover
            raise AssertionError("hard_sync should wrap token-mint failure in SyncError")
    assert calls == []
