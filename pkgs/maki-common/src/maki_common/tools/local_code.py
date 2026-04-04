"""Local filesystem + git tools — generic code read/write/search for any repo."""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

MAX_READ_LINES = 500
MAX_SEARCH_RESULTS = 30


def _safe_path(repo_path: str, relative: str) -> Path | None:
    """Resolve a relative path within repo_path, rejecting traversal."""
    base = Path(repo_path).resolve()
    target = (base / relative).resolve()
    if not str(target).startswith(str(base)):
        return None
    return target


async def _run_git(repo_path: str, *args: str) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def _run_cmd(repo_path: str, *args: str) -> tuple[int, str, str]:
    """Run an arbitrary command in the repo directory and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


def make_code_tools(
    repo_path: str,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Read-only code tools — work on any git repo.

    Tools: read_file, list_directory, search_text, git_status, git_diff.
    """

    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        """Read a file from the repository."""
        path = args.get("path", "")
        offset = int(args.get("offset", 0) or 0)
        limit = int(args.get("limit", 0) or 0)
        log.info("Tool: read_file", extra={"path": path, "offset": offset, "limit": limit})
        resolved = _safe_path(repo_path, path)
        if not resolved:
            return mcp_result(f"Error: path '{path}' is outside the repository.")
        if not resolved.is_file():
            return mcp_result(f"Error: '{path}' does not exist or is not a file.")
        try:
            all_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(all_lines)

            # Apply offset (1-based) and limit
            start = max(0, offset - 1) if offset > 0 else 0
            max_lines = limit if limit > 0 else MAX_READ_LINES
            end = min(start + max_lines, total)
            lines = all_lines[start:end]

            numbered = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(lines))
            if end < total:
                return mcp_result(f"{numbered}\n\n... truncated ({total} total lines, showing {start + 1}-{end})")
            if start > 0:
                return mcp_result(f"{numbered}\n\n(lines {start + 1}-{end} of {total})")
            return mcp_result(numbered)
        except Exception as e:
            return mcp_result(f"Error reading file: {e}")

    async def list_directory(args: dict[str, Any]) -> dict[str, Any]:
        """List contents of a directory in the repository."""
        path = args.get("path", "")
        log.info("Tool: list_directory", extra={"path": path})
        resolved = _safe_path(repo_path, path) if path else Path(repo_path).resolve()
        if not resolved:
            return mcp_result(f"Error: path '{path}' is outside the repository.")
        if not resolved.is_dir():
            return mcp_result(f"Error: '{path}' does not exist or is not a directory.")
        try:
            entries = sorted(resolved.iterdir())
            lines = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                kind = "d" if entry.is_dir() else "f"
                lines.append(f"{kind}  {entry.name}")
            return mcp_result("\n".join(lines) if lines else "(empty directory)")
        except Exception as e:
            return mcp_result(f"Error listing directory: {e}")

    async def search_text(args: dict[str, Any]) -> dict[str, Any]:
        """Search for text patterns in the repository."""
        query = args.get("query", "")
        path_filter = args.get("path", "")
        log.info("Tool: search_text", extra={"query": query, "path": path_filter})
        if not query:
            return mcp_result("Error: query is required.")
        try:
            search_path = repo_path
            if path_filter:
                resolved = _safe_path(repo_path, path_filter)
                if not resolved:
                    return mcp_result(f"Error: path '{path_filter}' is outside the repository.")
                search_path = str(resolved)

            proc = await asyncio.create_subprocess_exec(
                "grep",
                "-rn",
                "--include=*.py",
                "--include=*.yaml",
                "--include=*.yml",
                "--include=*.toml",
                "--include=*.json",
                "--include=*.md",
                "--include=*.txt",
                "--include=*.cfg",
                "--include=*.ini",
                "--include=*.sh",
                "--include=*.go",
                "--include=*.js",
                "--include=*.ts",
                "-C",
                "2",
                "-m",
                str(MAX_SEARCH_RESULTS),
                query,
                search_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            output = stdout.decode(errors="replace")

            if not output.strip():
                return mcp_result(f"No matches found for '{query}'.")

            # Make paths relative to repo
            base = str(repo_path)
            if base and not base.endswith("/"):
                base += "/"
            output = output.replace(base, "")

            return mcp_result(output)
        except Exception as e:
            return mcp_result(f"Error searching: {e}")

    async def git_status(args: dict[str, Any]) -> dict[str, Any]:
        """Show git status."""
        log.info("Tool: git_status")
        rc, stdout, stderr = await _run_git(repo_path, "status", "--short")
        if rc != 0:
            return mcp_result(f"Error: {stderr}")
        return mcp_result(stdout if stdout.strip() else "Working tree clean.")

    async def git_diff(args: dict[str, Any]) -> dict[str, Any]:
        """Show git diff of changes."""
        path = args.get("path", "")
        log.info("Tool: git_diff", extra={"path": path})
        cmd = ["diff"]
        if path:
            resolved = _safe_path(repo_path, path)
            if not resolved:
                return mcp_result(f"Error: path '{path}' is outside the repository.")
            cmd.append("--")
            cmd.append(path)
        rc, stdout, stderr = await _run_git(repo_path, *cmd)
        if rc != 0:
            return mcp_result(f"Error: {stderr}")
        return mcp_result(stdout if stdout.strip() else "No changes.")

    async def git_run(args: dict[str, Any]) -> dict[str, Any]:
        """Run an arbitrary git command."""
        raw_args = args.get("args", "")
        log.info("Tool: git_run", extra={"args": raw_args})
        if not raw_args.strip():
            return mcp_result("Error: args is required (e.g. 'status', 'log --oneline -10').")
        try:
            parts = shlex.split(raw_args)
        except ValueError as e:
            return mcp_result(f"Error parsing args: {e}")
        rc, stdout, stderr = await _run_git(repo_path, *parts)
        if rc != 0:
            return mcp_result(f"Exit {rc}: {stderr}" if stderr.strip() else f"Exit {rc}: {stdout}")
        return mcp_result(stdout if stdout.strip() else "(no output)")

    return [
        (
            "read_file",
            f"Read a file from the repository. Path is relative to repo root. "
            f"Returns line-numbered content (max {MAX_READ_LINES} lines per call). "
            f"Use 'offset' (1-based line number) and 'limit' to read specific sections of large files.",
            {"path": str, "offset": str, "limit": str},
            read_file,
        ),
        (
            "list_directory",
            "List contents of a directory. Empty path lists repo root. Shows 'd' for directories, 'f' for files.",
            {"path": str},
            list_directory,
        ),
        (
            "search_text",
            "Search for text patterns in the repository (grep-style). "
            "Returns matching lines with context. Optionally filter by path.",
            {"query": str, "path": str},
            search_text,
        ),
        (
            "git_run",
            "Run any git command. Pass the arguments as a string (everything after 'git'). "
            "Examples: 'status', 'log --oneline -10', 'diff HEAD~1', 'show HEAD:path/to/file', "
            "'blame path/to/file', 'branch -a'.",
            {"args": str},
            git_run,
        ),
    ]


def make_code_edit_tools(
    repo_path: str,
    github_auth: Any | None = None,
    repo_owner: str = "",
    repo_name: str = "",
    on_commit_success: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Write/commit/push tools — work on any git repo.

    Tools: write_file, git_commit_and_push, git_pull, quality_check.

    Args:
        repo_path: Absolute path to the local git repo.
        github_auth: GitHubAuth instance for push authentication (optional).
        repo_owner: Repo owner (for remote URL on push).
        repo_name: Repo name (for remote URL on push).
        on_commit_success: Optional async callback(sha, message, repo) fired after a
            successful push. Use this to persist episodic memory of what changed and where.
    """

    async def write_file(args: dict[str, Any]) -> dict[str, Any]:
        """Write a file to the repository."""
        path = args.get("path", "")
        content = args.get("content", "")
        log.info("Tool: write_file", extra={"path": path, "content_len": len(content)})
        resolved = _safe_path(repo_path, path)
        if not resolved:
            return mcp_result(f"Error: path '{path}' is outside the repository.")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return mcp_result(f"Written {len(content)} bytes to {path}")
        except Exception as e:
            return mcp_result(f"Error writing file: {e}")

    async def edit_file(args: dict[str, Any]) -> dict[str, Any]:
        """Edit a file using search-and-replace."""
        path = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        log.info("Tool: edit_file", extra={"path": path, "old_len": len(old_text), "new_len": len(new_text)})
        if not old_text:
            return mcp_result("Error: old_text is required.")
        resolved = _safe_path(repo_path, path)
        if not resolved:
            return mcp_result(f"Error: path '{path}' is outside the repository.")
        if not resolved.is_file():
            return mcp_result(f"Error: '{path}' does not exist or is not a file.")
        try:
            content = resolved.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                return mcp_result("Error: old_text not found in file. Provide exact text including whitespace.")
            if count > 1:
                return mcp_result(
                    f"Error: old_text found {count} times — ambiguous. Include more context to make it unique."
                )
            # Find line number of the match
            before = content[: content.index(old_text)]
            line_num = before.count("\n") + 1
            new_content = content.replace(old_text, new_text, 1)
            resolved.write_text(new_content, encoding="utf-8")
            return mcp_result(f"Edited {path} at line {line_num} ({len(old_text)} chars -> {len(new_text)} chars)")
        except Exception as e:
            return mcp_result(f"Error editing file: {e}")

    async def git_commit_and_push(args: dict[str, Any]) -> dict[str, Any]:
        """Stage files, commit, and push to remote."""
        message = args.get("message", "")
        files = args.get("files", "")
        log.info("Tool: git_commit_and_push", extra={"commit_msg": message, "files": files})
        if not message:
            return mcp_result("Error: commit message is required.")
        if not files:
            return mcp_result("Error: files to stage are required (comma-separated).")

        try:
            # Stage files
            file_list = [f.strip() for f in files.split(",") if f.strip()]
            for f in file_list:
                resolved = _safe_path(repo_path, f)
                if not resolved:
                    return mcp_result(f"Error: file '{f}' is outside the repository.")
                rc, _, stderr = await _run_git(repo_path, "add", f)
                if rc != 0:
                    return mcp_result(f"Error staging {f}: {stderr}")

            # Commit
            rc, stdout, stderr = await _run_git(repo_path, "commit", "-m", message)
            if rc != 0:
                return mcp_result(f"Commit failed: {stderr}")

            # Set remote URL with fresh token for push
            if github_auth and repo_owner and repo_name:
                token = await github_auth.get_token()
                remote_url = f"https://x-access-token:{token}@github.com/{repo_owner}/{repo_name}.git"
                await _run_git(repo_path, "remote", "set-url", "origin", remote_url)

            # Push
            rc, stdout, stderr = await _run_git(repo_path, "push", "origin", "main")
            if rc != 0:
                return mcp_result(f"Push failed: {stderr}")

            # Get commit SHA
            _, sha, _ = await _run_git(repo_path, "rev-parse", "--short", "HEAD")
            sha = sha.strip()

            # Fire episodic memory callback — non-blocking, never fail the commit
            if on_commit_success is not None:
                try:
                    repo_url = f"https://github.com/{repo_owner}/{repo_name}" if repo_owner and repo_name else repo_name
                    await on_commit_success(sha, message, repo_url)
                except Exception:
                    log.warning("on_commit_success callback failed", exc_info=True)

            return mcp_result(f"Committed and pushed ({sha}): {message}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def git_pull(args: dict[str, Any]) -> dict[str, Any]:
        """Pull latest changes from remote."""
        log.info("Tool: git_pull")
        try:
            if github_auth and repo_owner and repo_name:
                token = await github_auth.get_token()
                remote_url = f"https://x-access-token:{token}@github.com/{repo_owner}/{repo_name}.git"
                await _run_git(repo_path, "remote", "set-url", "origin", remote_url)

            rc, stdout, stderr = await _run_git(repo_path, "pull", "--rebase", "origin", "main")
            if rc != 0:
                if "rebase-merge" in stderr or "rebase" in stderr.lower():
                    log.warning("Stuck rebase detected, aborting and retrying with merge")
                    await _run_git(repo_path, "rebase", "--abort")
                    rc, stdout, stderr = await _run_git(repo_path, "pull", "origin", "main")
                if rc != 0:
                    return mcp_result(f"Pull failed: {stderr}")
            return mcp_result(stdout if stdout.strip() else "Already up to date.")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def quality_check(args: dict[str, Any]) -> dict[str, Any]:
        """Run linting and formatting checks on changed files before pushing.

        Runs ruff check (linting) and ruff format --check (formatting) on
        the pkgs/ directory. Returns pass/fail with details of any issues.
        Use this BEFORE git_commit_and_push to catch CI failures early.
        """
        path_filter = args.get("path", "pkgs/")
        log.info("Tool: quality_check", extra={"path": path_filter})

        results = []
        all_passed = True

        # Run ruff lint check
        try:
            rc, stdout, stderr = await _run_cmd(repo_path, "uvx", "ruff", "check", path_filter)
            if rc == 0:
                results.append("✅ ruff check (lint): passed")
            else:
                all_passed = False
                output = stdout or stderr
                results.append(f"❌ ruff check (lint): FAILED\n{output}")
        except FileNotFoundError:
            results.append("⚠️ uvx not found — install uv: https://docs.astral.sh/uv/")
            all_passed = False

        # Run ruff format check
        try:
            rc, stdout, stderr = await _run_cmd(repo_path, "uvx", "ruff", "format", "--check", path_filter)
            if rc == 0:
                results.append("✅ ruff format: passed")
            else:
                all_passed = False
                output = stdout or stderr
                results.append(f"❌ ruff format: FAILED\n{output}")
        except FileNotFoundError:
            results.append("⚠️ uvx not found — install uv: https://docs.astral.sh/uv/")
            all_passed = False

        # Run ty type check
        try:
            rc, stdout, stderr = await _run_cmd(repo_path, "uvx", "ty", "check", path_filter)
            if rc == 0:
                results.append("✅ ty check (types): passed")
            else:
                all_passed = False
                output = stdout or stderr
                results.append(f"❌ ty check (types): FAILED\n{output}")
        except FileNotFoundError:
            results.append("⚠️ uvx not found — install uv: https://docs.astral.sh/uv/")
            all_passed = False

        summary = "ALL CHECKS PASSED ✅" if all_passed else "CHECKS FAILED ❌ — fix issues before pushing"
        return mcp_result(f"{summary}\n\n" + "\n\n".join(results))

    return [
        (
            "write_file",
            "Write content to a file in the repository. Path is relative to repo root. "
            "Creates parent directories if needed. Provide the full file content.",
            {"path": str, "content": str},
            write_file,
        ),
        (
            "edit_file",
            "Edit a file using search-and-replace. Provide the exact old_text to find and new_text to replace it with. "
            "old_text must match exactly (including whitespace/indentation) and appear exactly once. "
            "Much more efficient than read_file + write_file for small changes.",
            {"path": str, "old_text": str, "new_text": str},
            edit_file,
        ),
        (
            "git_commit_and_push",
            "Stage specified files, commit with a message, and push to remote. "
            "Files should be comma-separated relative paths.",
            {"message": str, "files": str},
            git_commit_and_push,
        ),
        (
            "git_pull",
            "Pull latest changes from the remote repository.",
            {},
            git_pull,
        ),
        (
            "quality_check",
            "Run ruff lint, ruff format, and ty type checks on the codebase. "
            "Call this BEFORE git_commit_and_push to catch CI failures early. "
            "Optionally pass a path to check (default: pkgs/).",
            {"path": str},
            quality_check,
        ),
    ]
