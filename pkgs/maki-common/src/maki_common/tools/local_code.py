"""Local filesystem + git tools — generic code read/write/search across any registered repo.

Every tool accepts an optional `repo` arg (e.g. `"charmarr/charmarr"` or just
`"charmarr"`). When omitted, operations target the registry's default repo —
preserving the original maki-only behaviour without breaking callers.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from maki_common.repo import RepoEntry, RepoRegistry, _auth_config_args, redact_token
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


async def _run_git(
    repo_path: str,
    *args: str,
    token: str | None = None,
) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr).

    When *token* is supplied, the GitHub installation token is injected via
    ``git -c http.extraheader=...`` so it authenticates this single
    invocation without ever landing in ``<repo>/.git/config`` (issue #347).
    Local-only operations (status, diff, add, commit, rev-parse) don't
    need a token; remote operations (push, pull, fetch) do.

    stdout/stderr are pre-redacted of any GitHub token — git's own error
    messages echo the full remote URL (including `x-access-token:TOKEN@`),
    and that text flows into both structured logs and MCP results.
    """
    cmd_args: list[str] = []
    if token:
        cmd_args.extend(_auth_config_args(token))
    cmd_args.extend(["-C", repo_path, *args])
    proc = await asyncio.create_subprocess_exec(
        "git",
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, redact_token(stdout.decode()), redact_token(stderr.decode())


async def _run_cmd(repo_path: str, *args: str) -> tuple[int, str, str]:
    """Run an arbitrary command in the repo directory and return (returncode, stdout, stderr).

    Output is pre-redacted defensively — most callers run linters, but anything
    that shells out near a git context can pick up a token from environment or
    error chaining.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, redact_token(stdout.decode()), redact_token(stderr.decode())


async def _resolve(registry: RepoRegistry, args: dict[str, Any]) -> tuple[RepoEntry | None, str | None]:
    """Resolve the `repo` arg to a RepoEntry; return (entry, error_message)."""
    repo_key = (args.get("repo") or "").strip() or None
    entry = await registry.resolve(repo_key)
    if entry is None:
        if repo_key:
            return None, (
                f"Error: unknown or unreachable repo '{repo_key}'. "
                f"Pass 'owner/name' to clone on demand. Known: {', '.join(registry.known()) or '(none)'}"
            )
        return None, "Error: no default repo registered for this server."
    return entry, None


def make_code_tools(
    registry: RepoRegistry,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Read-only code tools — accept an optional `repo` arg; default to the registry's primary repo.

    Tools: read_file, list_directory, search_text, git_status, git_diff.
    """

    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        """Read a file from a repository."""
        path = args.get("path", "")
        offset = int(args.get("offset", 0) or 0)
        limit = int(args.get("limit", 0) or 0)
        log.info("Tool: read_file", extra={"path": path, "offset": offset, "limit": limit, "repo": args.get("repo")})
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        resolved = _safe_path(entry.path, path)
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
        """List contents of a directory in a repository."""
        path = args.get("path", "")
        log.info("Tool: list_directory", extra={"path": path, "repo": args.get("repo")})
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        resolved = _safe_path(entry.path, path) if path else Path(entry.path).resolve()
        if not resolved:
            return mcp_result(f"Error: path '{path}' is outside the repository.")
        if not resolved.is_dir():
            return mcp_result(f"Error: '{path}' does not exist or is not a directory.")
        try:
            entries = sorted(resolved.iterdir())
            lines = []
            for direntry in entries:
                if direntry.name.startswith("."):
                    continue
                kind = "d" if direntry.is_dir() else "f"
                lines.append(f"{kind}  {direntry.name}")
            return mcp_result("\n".join(lines) if lines else "(empty directory)")
        except Exception as e:
            return mcp_result(f"Error listing directory: {e}")

    async def search_text(args: dict[str, Any]) -> dict[str, Any]:
        """Search for text patterns in a repository."""
        query = args.get("query", "")
        path_filter = args.get("path", "")
        log.info("Tool: search_text", extra={"query": query, "path": path_filter, "repo": args.get("repo")})
        if not query:
            return mcp_result("Error: query is required.")
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        try:
            search_path = entry.path
            if path_filter:
                resolved = _safe_path(entry.path, path_filter)
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
            base = str(entry.path)
            if base and not base.endswith("/"):
                base += "/"
            output = output.replace(base, "")

            return mcp_result(output)
        except Exception as e:
            return mcp_result(f"Error searching: {e}")

    async def git_status(args: dict[str, Any]) -> dict[str, Any]:
        """Show git status."""
        log.info("Tool: git_status", extra={"repo": args.get("repo")})
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        rc, stdout, stderr = await _run_git(entry.path, "status", "--short")
        if rc != 0:
            return mcp_result(f"Error: {stderr}")
        return mcp_result(stdout if stdout.strip() else "Working tree clean.")

    async def git_diff(args: dict[str, Any]) -> dict[str, Any]:
        """Show git diff of changes."""
        path = args.get("path", "")
        log.info("Tool: git_diff", extra={"path": path, "repo": args.get("repo")})
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        cmd = ["diff"]
        if path:
            resolved = _safe_path(entry.path, path)
            if not resolved:
                return mcp_result(f"Error: path '{path}' is outside the repository.")
            cmd.append("--")
            cmd.append(path)
        rc, stdout, stderr = await _run_git(entry.path, *cmd)
        if rc != 0:
            return mcp_result(f"Error: {stderr}")
        return mcp_result(stdout if stdout.strip() else "No changes.")

    return [
        (
            "read_file",
            f"Read a file from a repository. Path is relative to repo root. "
            f"Returns line-numbered content (max {MAX_READ_LINES} lines per call). "
            f"Use 'offset' (1-based line number) and 'limit' to read specific sections of large files. "
            f"Optional `repo` arg (e.g. 'owner/name' or short name) selects a non-default repo; "
            f"unknown owner/name pairs are cloned on demand.",
            {"path": str, "offset": str, "limit": str, "repo": str},
            read_file,
        ),
        (
            "list_directory",
            "List contents of a directory. Empty path lists repo root. Shows 'd' for directories, "
            "'f' for files. Optional `repo` arg selects a non-default repo.",
            {"path": str, "repo": str},
            list_directory,
        ),
        (
            "search_text",
            "Search for text patterns in a repository (grep-style). "
            "Returns matching lines with context. Optionally filter by path. "
            "Optional `repo` arg selects a non-default repo.",
            {"query": str, "path": str, "repo": str},
            search_text,
        ),
        (
            "git_status",
            "Show git status (short format). Reports 'Working tree clean.' when there are no changes. "
            "Optional `repo` arg selects a non-default repo.",
            {"repo": str},
            git_status,
        ),
        (
            "git_diff",
            "Show unstaged git diff. Optionally pass a 'path' to limit the diff to that file/directory. "
            "Optional `repo` arg selects a non-default repo.",
            {"path": str, "repo": str},
            git_diff,
        ),
    ]


def make_code_edit_tools(
    registry: RepoRegistry,
    on_commit_success: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Write/commit/push tools — accept an optional `repo` arg.

    Tools: write_file, edit_file, git_commit_and_push, git_pull, git_run, quality_check.

    Args:
        registry: Multi-repo workspace registry. Each tool's `repo` arg selects
            an entry; omitted/empty falls back to the default repo.
        on_commit_success: Optional async callback(sha, message, repo_url) fired after a
            successful push. Use this to persist episodic memory of what changed and where.
    """

    async def write_file(args: dict[str, Any]) -> dict[str, Any]:
        """Write a file to a repository."""
        path = args.get("path", "")
        content = args.get("content", "")
        log.info(
            "Tool: write_file",
            extra={"path": path, "content_len": len(content), "repo": args.get("repo")},
        )
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        resolved = _safe_path(entry.path, path)
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
        log.info(
            "Tool: edit_file",
            extra={
                "path": path,
                "old_len": len(old_text),
                "new_len": len(new_text),
                "repo": args.get("repo"),
            },
        )
        if not old_text:
            return mcp_result("Error: old_text is required.")
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        resolved = _safe_path(entry.path, path)
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
        log.info(
            "Tool: git_commit_and_push",
            extra={"commit_msg": message, "files": files, "repo": args.get("repo")},
        )
        if not message:
            return mcp_result("Error: commit message is required.")
        if not files:
            return mcp_result("Error: files to stage are required (comma-separated).")
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")

        try:
            # Stage files
            file_list = [f.strip() for f in files.split(",") if f.strip()]
            for f in file_list:
                resolved = _safe_path(entry.path, f)
                if not resolved:
                    return mcp_result(f"Error: file '{f}' is outside the repository.")
                rc, _, stderr = await _run_git(entry.path, "add", f)
                if rc != 0:
                    return mcp_result(f"Error staging {f}: {stderr}")

            # Commit — always as makiself[bot] regardless of local git config
            rc, stdout, stderr = await _run_git(
                entry.path,
                "commit",
                "--author",
                "makiself[bot] <makiself[bot]@users.noreply.github.com>",
                "-m",
                message,
            )
            if rc != 0:
                return mcp_result(f"Commit failed: {stderr}")

            # Mint a fresh installation token for the push. The token is
            # injected per-invocation via `git -c http.extraheader=...`
            # (issue #347) — never written to `.git/config`. We also rewrite
            # origin to the token-free URL to scrub any legacy embedded-token
            # URL written by older versions of this module.
            push_token: str | None = None
            if entry.auth and entry.owner and entry.name:
                push_token = await entry.auth.get_token()
                clean_url = f"https://github.com/{entry.owner}/{entry.name}.git"
                await _run_git(entry.path, "remote", "set-url", "origin", clean_url)

            # Push
            rc, stdout, stderr = await _run_git(entry.path, "push", "origin", "main", token=push_token)
            if rc != 0:
                return mcp_result(f"Push failed: {stderr}")

            # Get commit SHA
            _, sha, _ = await _run_git(entry.path, "rev-parse", "--short", "HEAD")
            sha = sha.strip()

            # Fire episodic memory callback — non-blocking, never fail the commit
            if on_commit_success is not None:
                try:
                    repo_url = (
                        f"https://github.com/{entry.owner}/{entry.name}" if entry.owner and entry.name else entry.name
                    )
                    await on_commit_success(sha, message, repo_url)
                except Exception:
                    log.warning("on_commit_success callback failed", exc_info=True)

            return mcp_result(f"Committed and pushed ({sha}) to {entry.owner}/{entry.name}: {message}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def git_pull(args: dict[str, Any]) -> dict[str, Any]:
        """Pull latest changes from remote."""
        log.info("Tool: git_pull", extra={"repo": args.get("repo")})
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        try:
            # Mint a fresh installation token for the pull. Injected
            # per-invocation via `git -c http.extraheader=...` (issue #347)
            # — never written to `.git/config`. We also rewrite origin to
            # the token-free URL to scrub any legacy embedded-token URL.
            pull_token: str | None = None
            if entry.auth and entry.owner and entry.name:
                pull_token = await entry.auth.get_token()
                clean_url = f"https://github.com/{entry.owner}/{entry.name}.git"
                await _run_git(entry.path, "remote", "set-url", "origin", clean_url)

            # Clear stuck rebase state before pulling — rebase --abort silently
            # fails in some states, so nuke the directory directly.
            import shutil

            for rebase_dir in ("rebase-merge", "rebase-apply"):
                p = Path(entry.path) / ".git" / rebase_dir
                if p.is_dir():
                    shutil.rmtree(p)
                    log.warning("Cleared stuck rebase dir", extra={"path": str(p)})

            rc, stdout, stderr = await _run_git(entry.path, "pull", "--rebase", "origin", "main", token=pull_token)
            if rc != 0:
                if "rebase" in stderr.lower():
                    log.warning("Rebase failed, retrying with merge")
                    rc, stdout, stderr = await _run_git(entry.path, "pull", "origin", "main", token=pull_token)
                if rc != 0:
                    return mcp_result(f"Pull failed: {stderr}")
            return mcp_result(stdout if stdout.strip() else "Already up to date.")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def git_run(args: dict[str, Any]) -> dict[str, Any]:
        """Run an arbitrary git command in the repo workspace.

        Useful for read-mostly operations (`log`, `show`, `branch -a`, `rev-parse`)
        and ad-hoc git that doesn't have a dedicated tool. The command is parsed
        with shlex; `git` is prepended automatically.
        """
        command = args.get("command", "")
        log.info("Tool: git_run", extra={"command": command, "repo": args.get("repo")})
        if not command:
            return mcp_result("Error: command is required (e.g. 'log -n 5 --oneline').")
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")
        try:
            parts = shlex.split(command)
        except ValueError as e:
            return mcp_result(f"Error parsing command: {e}")
        if not parts:
            return mcp_result("Error: empty command after parsing.")
        # Strip a leading 'git' if the caller included it.
        if parts[0] == "git":
            parts = parts[1:]
        if not parts:
            return mcp_result("Error: command is just 'git' with no subcommand.")
        rc, stdout, stderr = await _run_git(entry.path, *parts)
        output = stdout if stdout.strip() else stderr
        if rc != 0:
            return mcp_result(f"git {command} failed (exit {rc}):\n{output}")
        return mcp_result(output if output.strip() else "(no output)")

    async def quality_check(args: dict[str, Any]) -> dict[str, Any]:
        """Run linting and formatting checks on changed files before pushing.

        Runs ruff check (linting) and ruff format --check (formatting) on
        the pkgs/ directory by default. Returns pass/fail with details of any
        issues. Use this BEFORE git_commit_and_push to catch CI failures early.
        Optional `repo` arg targets a non-default repo.
        """
        path_filter = args.get("path", "pkgs/")
        log.info("Tool: quality_check", extra={"path": path_filter, "repo": args.get("repo")})
        entry, err = await _resolve(registry, args)
        if entry is None:
            return mcp_result(err or "")

        results = []
        all_passed = True

        # Run ruff lint check
        try:
            rc, stdout, stderr = await _run_cmd(entry.path, "uvx", "ruff", "check", path_filter)
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
            rc, stdout, stderr = await _run_cmd(entry.path, "uvx", "ruff", "format", "--check", path_filter)
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
            rc, stdout, stderr = await _run_cmd(entry.path, "uvx", "ty", "check", path_filter)
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
            "Write content to a file in a repository. Path is relative to repo root. "
            "Creates parent directories if needed. Provide the full file content. "
            "Optional `repo` arg (e.g. 'owner/name' or short name) selects a non-default repo.",
            {"path": str, "content": str, "repo": str},
            write_file,
        ),
        (
            "edit_file",
            "Edit a file using search-and-replace. Provide the exact old_text to find and new_text to replace it with. "
            "old_text must match exactly (including whitespace/indentation) and appear exactly once. "
            "Much more efficient than read_file + write_file for small changes. "
            "Optional `repo` arg selects a non-default repo.",
            {"path": str, "old_text": str, "new_text": str, "repo": str},
            edit_file,
        ),
        (
            "git_commit_and_push",
            "Stage specified files, commit with a message, and push to remote. "
            "Files should be comma-separated relative paths. "
            "Optional `repo` arg selects a non-default repo (uses that repo's auth for push).",
            {"message": str, "files": str, "repo": str},
            git_commit_and_push,
        ),
        (
            "git_pull",
            "Pull latest changes from the remote repository. Optional `repo` arg selects a non-default repo.",
            {"repo": str},
            git_pull,
        ),
        (
            "git_run",
            "Run an arbitrary git command in the repo workspace (e.g. 'log -n 5 --oneline', "
            "'branch -a', 'show HEAD'). The leading 'git' is added automatically. "
            "Optional `repo` arg selects a non-default repo.",
            {"command": str, "repo": str},
            git_run,
        ),
        (
            "quality_check",
            "Run ruff lint, ruff format, and ty type checks on the codebase. "
            "Call this BEFORE git_commit_and_push to catch CI failures early. "
            "Optionally pass a path to check (default: pkgs/). "
            "Optional `repo` arg selects a non-default repo.",
            {"path": str, "repo": str},
            quality_check,
        ),
    ]
