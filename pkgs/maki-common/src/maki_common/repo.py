"""Shared git repository initialization — clone or pull."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)


# GitHub installation tokens leak via git's own error messages, e.g.
#   fatal: unable to access 'https://x-access-token:ghs_xxxxx@github.com/...': 403
# Strip the secret before anything reaches a log aggregator or an MCP result.
_TOKEN_URL_RE = re.compile(r"x-access-token:[^@\s/]+@")
# Raw GitHub token prefixes — installation (ghs_), personal (ghp_), user (gho_),
# refresh (ghr_), server-to-server (ghu_). Belt-and-suspenders in case the token
# ever shows up outside the URL form.
_BARE_TOKEN_RE = re.compile(r"\bgh[suporf]_[A-Za-z0-9]{20,}")


def redact_token(text: str) -> str:
    """Strip GitHub installation tokens from arbitrary text.

    Handles the `x-access-token:TOKEN@host` URL form (the form we inject) and
    the bare `ghs_*` / `ghp_*` / `gho_*` / `ghu_*` / `ghr_*` token prefixes as
    a defensive fallback. Safe to call on stdout, stderr, log strings, MCP
    results — any caller-visible output that may have come from a git process.
    """
    if not text:
        return text
    text = _TOKEN_URL_RE.sub("x-access-token:***@", text)
    text = _BARE_TOKEN_RE.sub("***", text)
    return text


async def _run_git(repo_path: str, *args: str) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr).

    stdout and stderr are pre-redacted — any embedded GitHub token (in URLs
    git echoes back in errors, or otherwise) is stripped before the bytes
    leave this function. Every caller — logger, MCP tool, exception path —
    therefore sees safe text by construction.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, redact_token(stdout.decode()), redact_token(stderr.decode())


async def _run_git_no_cwd(*args: str) -> tuple[int, str, str]:
    """Run a git command without a `-C` working directory (e.g. clone).

    Same redaction guarantees as `_run_git`.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, redact_token(stdout.decode()), redact_token(stderr.decode())


async def init_repo(
    repo_path: str,
    clone_url: str,
    github_auth: Any | None = None,
    git_user: str = "makiself[bot]",
    git_email: str = "makiself[bot]@users.noreply.github.com",
) -> None:
    """Clone or pull a git repo.

    Args:
        repo_path: Local path for the clone.
        clone_url: HTTPS clone URL (e.g. https://github.com/owner/repo.git).
        github_auth: GitHubAuth instance — if provided, injects token into URL for auth.
        git_user: Git user.name for commits.
        git_email: Git user.email for commits.
    """
    auth_url = clone_url
    if github_auth:
        try:
            token = await github_auth.get_token()
            # Inject token: https://github.com/... → https://x-access-token:TOKEN@github.com/...
            auth_url = clone_url.replace("https://", f"https://x-access-token:{token}@")
        except Exception:
            log.warning("Failed to get GitHub token, cloning without auth")

    if not os.path.exists(os.path.join(repo_path, ".git")):
        log.info("Cloning repo", extra={"repo_path": repo_path})
        os.makedirs(os.path.dirname(repo_path), exist_ok=True)
        rc, _, stderr = await _run_git_no_cwd("clone", auth_url, repo_path)
        if rc != 0:
            log.error("Git clone failed", extra={"stderr": stderr})
            return
        log.info("Repo cloned", extra={"repo_path": repo_path})

        # Configure git identity
        await _run_git(repo_path, "config", "user.name", git_user)
        await _run_git(repo_path, "config", "user.email", git_email)
    else:
        log.info("Pulling latest", extra={"repo_path": repo_path})
        if github_auth:
            await _run_git(repo_path, "remote", "set-url", "origin", auth_url)
        rc, _, stderr = await _run_git(repo_path, "pull", "--rebase", "origin", "main")
        if rc != 0:
            log.warning("Git pull failed", extra={"stderr": stderr})
        else:
            log.info("Repo updated", extra={"repo_path": repo_path})
