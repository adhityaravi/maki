"""Tests for maki_common.tools.local_code — path-safety gatekeeper.

`_safe_path` is the single choke-point every filesystem tool routes through,
so regressions here would let a caller read or write outside the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

from maki_common.tools.local_code import _safe_path


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
