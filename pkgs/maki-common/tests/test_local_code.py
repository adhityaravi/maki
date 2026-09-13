"""Tests for maki_common.tools.local_code — path-safety gatekeeper.

`_safe_path` is the single choke-point every filesystem tool routes through,
so regressions here would let a caller read or write outside the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

from maki_common.tools.local_code import _path_touches_git, _safe_path, _writable_path


def test_safe_path_accepts_in_repo(tmp_path: Path) -> None:
    """A vanilla relative path resolves inside the repo."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    resolved = _safe_path(str(tmp_path), "src/a.py")
    assert resolved is not None
    assert resolved == (tmp_path / "src" / "a.py").resolve()


def test_safe_path_accepts_repo_root(tmp_path: Path) -> None:
    """The base itself is legal — some callers pass ``""`` or ``"."``."""
    for rel in ("", "."):
        resolved = _safe_path(str(tmp_path), rel)
        assert resolved is not None
        assert resolved == tmp_path.resolve()


def test_safe_path_rejects_parent_traversal(tmp_path: Path) -> None:
    """``../secret`` must not escape."""
    base = tmp_path / "repo"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    assert _safe_path(str(base), "../secret.txt") is None


def test_safe_path_rejects_absolute_outside(tmp_path: Path) -> None:
    """An absolute path outside the base is rejected (not silently accepted)."""
    base = tmp_path / "repo"
    base.mkdir()
    outside = tmp_path / "other" / "x.txt"
    assert _safe_path(str(base), str(outside)) is None


def test_safe_path_rejects_sibling_prefix(tmp_path: Path) -> None:
    """Regression: a sibling dir whose name shares a prefix must not slip through.

    Old ``str(target).startswith(str(base))`` said yes to
    ``/tmp/maki-evil`` when base was ``/tmp/maki``. is_relative_to fixes it.
    """
    base = tmp_path / "maki"
    base.mkdir()
    evil = tmp_path / "maki-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text("stolen")
    # relative form: `..`` then into the sibling
    assert _safe_path(str(base), "../maki-evil/secret.txt") is None
    # absolute form: same target passed directly
    assert _safe_path(str(base), str(evil / "secret.txt")) is None


def test_safe_path_accepts_absolute_inside(tmp_path: Path) -> None:
    """An absolute path that happens to be inside the repo is fine —
    the check is on the resolved location, not on how it was spelled."""
    base = tmp_path / "repo"
    base.mkdir()
    inside = base / "sub" / "a.py"
    inside.parent.mkdir()
    inside.write_text("")
    resolved = _safe_path(str(base), str(inside))
    assert resolved == inside.resolve()


def test_safe_path_symlink_escape(tmp_path: Path) -> None:
    """Best-effort: a symlink pointing outside the repo resolves outside and is rejected."""
    base = tmp_path / "repo"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("stolen")
    link = base / "escape"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return  # symlinks not supported on this platform — nothing to verify
    assert _safe_path(str(base), "escape/secret.txt") is None


# ---------------------------------------------------------------------------
# _writable_path — extra `.git/` ban on top of the traversal guard. Reads
# still use _safe_path so they can inspect .git; only mutations route here.
# ---------------------------------------------------------------------------


def test_writable_path_allows_normal_file(tmp_path: Path) -> None:
    """Ordinary in-repo paths still resolve — the guard only shifts for .git."""
    (tmp_path / "src").mkdir()
    resolved = _writable_path(str(tmp_path), "src/a.py")
    assert resolved == (tmp_path / "src" / "a.py").resolve()


def test_writable_path_rejects_dot_git_config(tmp_path: Path) -> None:
    """Rewriting `.git/config` would let an attacker point origin elsewhere."""
    (tmp_path / ".git").mkdir()
    assert _writable_path(str(tmp_path), ".git/config") is None


def test_writable_path_rejects_dot_git_head(tmp_path: Path) -> None:
    """Nuking `HEAD` detaches the branch and can lose commits."""
    (tmp_path / ".git").mkdir()
    assert _writable_path(str(tmp_path), ".git/HEAD") is None


def test_writable_path_rejects_dot_git_hook(tmp_path: Path) -> None:
    """A poisoned pre-commit hook fires on the very next commit tool call."""
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    assert _writable_path(str(tmp_path), ".git/hooks/pre-commit") is None


def test_writable_path_rejects_dot_git_refs(tmp_path: Path) -> None:
    """Overwriting refs/heads/main rewrites branch history from under us."""
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    assert _writable_path(str(tmp_path), ".git/refs/heads/main") is None


def test_writable_path_rejects_nested_dot_git(tmp_path: Path) -> None:
    """Component-wise ban catches submodule-style nested .git dirs too."""
    (tmp_path / "vendor" / "sub" / ".git").mkdir(parents=True)
    assert _writable_path(str(tmp_path), "vendor/sub/.git/config") is None


def test_writable_path_rejects_dot_git_via_absolute(tmp_path: Path) -> None:
    """Absolute spelling of the same target is blocked — check is on resolved."""
    (tmp_path / ".git").mkdir()
    assert _writable_path(str(tmp_path), str(tmp_path / ".git" / "config")) is None


def test_writable_path_still_rejects_traversal(tmp_path: Path) -> None:
    """The base traversal guard from _safe_path still applies."""
    base = tmp_path / "repo"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    assert _writable_path(str(base), "../secret.txt") is None


def test_writable_path_allows_similar_but_not_dot_git(tmp_path: Path) -> None:
    """`.gitignore`, `.github/**`, `git/` aren't blocked — only exact `.git` component."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "git").mkdir()
    assert _writable_path(str(tmp_path), ".gitignore") is not None
    assert _writable_path(str(tmp_path), ".github/workflows/ci.yml") is not None
    assert _writable_path(str(tmp_path), "git/notes.md") is not None


# ---------------------------------------------------------------------------
# _path_touches_git — lets callers give a distinct error when we blocked
# for the .git reason vs. for the traversal reason.
# ---------------------------------------------------------------------------


def test_path_touches_git_true_for_git_child(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert _path_touches_git(str(tmp_path), ".git/config") is True


def test_path_touches_git_false_for_outside_repo(tmp_path: Path) -> None:
    """Outside-repo isn't a .git hit — caller should render the traversal error."""
    base = tmp_path / "repo"
    base.mkdir()
    assert _path_touches_git(str(base), "../elsewhere") is False


def test_path_touches_git_false_for_ordinary_file(tmp_path: Path) -> None:
    assert _path_touches_git(str(tmp_path), "src/a.py") is False
