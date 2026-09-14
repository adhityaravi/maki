"""Tests for maki_common.tools.local_code — path-safety gatekeeper.

`_safe_path` is the single choke-point every filesystem tool routes through,
so regressions here would let a caller read or write outside the repo.

Also covers `search_text` behaviour: the three silent-failure paths fixed in
#612 (per-file `-m` cap → total cap, extension whitelist → no whitelist,
grep rc=2 discarded → surfaced as an error).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from maki_common.repo import RepoEntry, RepoRegistry
from maki_common.tools.local_code import (
    MAX_SEARCH_RESULTS,
    _path_touches_git,
    _safe_path,
    _writable_path,
    make_code_tools,
)


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


# ---------------------------------------------------------------------------
# search_text — regression tests for the three silent-failure paths in #612.
# ---------------------------------------------------------------------------


def _search_text_tool(tmp_path: Path):
    """Build a `search_text` callable wired against `tmp_path` as the repo.

    `sync_ttl_seconds=inf` disables the auto-fetch that would trip on a
    fixture directory with no real `.git`.
    """
    registry = RepoRegistry(workspace_root=str(tmp_path), sync_ttl_seconds=float("inf"))
    registry.register(
        RepoEntry(path=str(tmp_path), owner="test", name="fixture"),
        default=True,
    )
    tools = make_code_tools(registry)
    for name, _desc, _schema, fn in tools:
        if name == "search_text":
            return fn
    raise AssertionError("search_text tool not registered")


def _result_text(result: dict) -> str:
    """Pull the text payload out of an MCP tool result dict."""
    return result["content"][0]["text"]


def test_search_text_finds_terraform_files(tmp_path: Path) -> None:
    """Regression for #612: `.tf` / `.hcl` were hidden by the old whitelist.

    `infra/` is 100% Terraform + Terragrunt; every infra grep returned
    "No matches" until the whitelist was dropped.
    """
    (tmp_path / "infra" / "modules").mkdir(parents=True)
    (tmp_path / "infra" / "root.hcl").write_text('locals {\n  db_name = "maki_vault"\n}\n')
    (tmp_path / "infra" / "modules" / "vault.tf").write_text('resource "kubernetes_deployment" "vault" {}\n')

    tool = _search_text_tool(tmp_path)
    result = asyncio.run(tool({"query": "maki_vault"}))
    text = _result_text(result)
    assert "root.hcl" in text
    assert "maki_vault" in text

    result_tf = asyncio.run(tool({"query": "kubernetes_deployment"}))
    text_tf = _result_text(result_tf)
    assert "vault.tf" in text_tf


def test_search_text_finds_dockerfiles_and_dotfiles(tmp_path: Path) -> None:
    """`Dockerfile`, `.gitignore`, `.env.example` — no extension, invisible before."""
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nRUN apt-get update\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (tmp_path / ".env.example").write_text("DATABASE_URL=postgres://localhost\n")

    tool = _search_text_tool(tmp_path)
    assert "Dockerfile" in _result_text(asyncio.run(tool({"query": "python:3.12-slim"})))
    assert ".gitignore" in _result_text(asyncio.run(tool({"query": "__pycache__"})))
    assert ".env.example" in _result_text(asyncio.run(tool({"query": "DATABASE_URL"})))


def test_search_text_total_match_cap_not_per_file(tmp_path: Path) -> None:
    """Regression for #612 bug 1: `-m N` is per-file, so N files × N matches
    each dumped N² lines. The Python cap is on TOTAL matches across all files.

    Fixture: many files each containing many hits. If the cap were per-file
    (or missing), we'd get files × per-file matches. With the total cap we
    get exactly ``MAX_SEARCH_RESULTS`` match lines regardless of file count.
    """
    per_file_hits = 5
    file_count = MAX_SEARCH_RESULTS  # guarantees > MAX_SEARCH_RESULTS total hits
    for i in range(file_count):
        lines = "\n".join(f"UNIQUETOKEN row {j}" for j in range(per_file_hits))
        (tmp_path / f"file_{i:03d}.py").write_text(lines + "\n")

    tool = _search_text_tool(tmp_path)
    text = _result_text(asyncio.run(tool({"query": "UNIQUETOKEN"})))

    # Match lines have grep's `path:LINE:content` shape. Context lines use
    # `path-LINE-content`. Count only match lines.
    import re

    match_lines = [ln for ln in text.splitlines() if re.match(r"^[^\n]+?:\d+:", ln)]
    assert len(match_lines) <= MAX_SEARCH_RESULTS, f"expected ≤{MAX_SEARCH_RESULTS} match lines, got {len(match_lines)}"
    # And we should have hit the cap (fixture guarantees >cap hits exist).
    assert "truncated" in text.lower()


def test_search_text_surfaces_grep_error_on_bad_regex(tmp_path: Path) -> None:
    """Regression for #612 comment (third bug): a malformed regex used to
    silently return "No matches found" because rc/stderr were discarded.
    """
    (tmp_path / "a.py").write_text("hello world\n")
    tool = _search_text_tool(tmp_path)
    # `(foo` is an unbalanced group — grep exits 2 with a parse error.
    result = asyncio.run(tool({"query": "(foo"}))
    text = _result_text(result)
    assert "Error" in text
    # Must NOT silently claim no matches — that was the bug.
    assert "No matches found" not in text


def test_search_text_returns_no_matches_when_absent(tmp_path: Path) -> None:
    """Sanity: the ordinary "no matches" case still says so (rc=1 not error)."""
    (tmp_path / "a.py").write_text("hello world\n")
    tool = _search_text_tool(tmp_path)
    result = asyncio.run(tool({"query": "definitelynothere"}))
    assert "No matches found" in _result_text(result)


def test_search_text_skips_vendored_dirs(tmp_path: Path) -> None:
    """`node_modules` / `__pycache__` / `.venv` shouldn't leak into results."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("MARKER = 1\n")
    (tmp_path / "node_modules" / "junk").mkdir(parents=True)
    (tmp_path / "node_modules" / "junk" / "index.js").write_text("var MARKER = 1;\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("MARKER = 2\n")

    tool = _search_text_tool(tmp_path)
    text = _result_text(asyncio.run(tool({"query": "MARKER"})))
    assert "src/app.py" in text
    assert "node_modules" not in text
    assert "__pycache__" not in text


def test_search_text_path_filter_scopes_search(tmp_path: Path) -> None:
    """Sanity: passing `path` narrows the search to that subtree."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "hit.py").write_text("NEEDLE\n")
    (tmp_path / "b" / "hit.py").write_text("NEEDLE\n")

    tool = _search_text_tool(tmp_path)
    text = _result_text(asyncio.run(tool({"query": "NEEDLE", "path": "a"})))
    assert "a/hit.py" in text
    assert "b/hit.py" not in text


def test_search_text_empty_query_errors(tmp_path: Path) -> None:
    tool = _search_text_tool(tmp_path)
    result = asyncio.run(tool({"query": ""}))
    assert "Error" in _result_text(result)


# `grep` must exist for the search_text tests to run — every dev/CI env has
# it, but skip cleanly on the rare exception rather than failing opaquely.
def _grep_missing() -> bool:
    from shutil import which

    return which("grep") is None


pytestmark = pytest.mark.skipif(_grep_missing(), reason="grep not installed on this system")
