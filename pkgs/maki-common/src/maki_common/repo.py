"""Shared git repository initialization — clone or pull."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


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
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            auth_url,
            repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("Git clone failed", extra={"stderr": stderr.decode()})
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
