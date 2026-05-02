"""Shared git repository initialization — clone or pull, plus a multi-repo workspace registry."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
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
) -> bool:
    """Clone or pull a git repo. Returns True on success.

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
        parent = os.path.dirname(repo_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        rc, _, stderr = await _run_git_no_cwd("clone", auth_url, repo_path)
        if rc != 0:
            log.error("Git clone failed", extra={"stderr": stderr})
            return False
        log.info("Repo cloned", extra={"repo_path": repo_path})

        # Configure git identity
        await _run_git(repo_path, "config", "user.name", git_user)
        await _run_git(repo_path, "config", "user.email", git_email)
        return True
    else:
        log.info("Pulling latest", extra={"repo_path": repo_path})
        if github_auth:
            await _run_git(repo_path, "remote", "set-url", "origin", auth_url)
        rc, _, stderr = await _run_git(repo_path, "pull", "--rebase", "origin", "main")
        if rc != 0:
            log.warning("Git pull failed", extra={"stderr": stderr})
            return False
        log.info("Repo updated", extra={"repo_path": repo_path})
        return True


# ---------------------------------------------------------------------------
# Multi-repo workspace registry
# ---------------------------------------------------------------------------


@dataclass
class RepoEntry:
    """One known git repo workspace.

    `path` is the local clone directory. `owner`/`name` identify the GitHub
    repo for push-URL construction. `auth` is the GitHubAuth used for clone
    and push; if None, operations run without injected credentials.
    """

    path: str
    owner: str
    name: str
    auth: Any | None = None
    clone_url: str | None = None

    def resolved_clone_url(self) -> str:
        return self.clone_url or f"https://github.com/{self.owner}/{self.name}.git"


class RepoRegistry:
    """Resolve a `repo` MCP-tool argument to a local workspace, cloning on demand.

    The cortex/immune service registers its primary repo (maki) as the default
    at startup. Tools that accept a `repo` arg call `await registry.resolve(arg)`:

        - `arg` empty/None  → default repo (no-arg behaviour preserved)
        - `arg = "name"`    → already-registered repo with that short name
        - `arg = "owner/name"` → fully-qualified; auto-registered + cloned at
          `/repo/<name>` (or `workspace_root/<name>`) using the default repo's
          GitHubAuth credentials

    Auto-clones inherit the default repo's auth — typically the makiself[bot]
    installation, which has access to every repo in adhityaravi/*. A repo the
    bot has no installation access to will fail at clone with a redacted error.
    """

    def __init__(self, workspace_root: str = "/repo") -> None:
        self._repos: dict[str, RepoEntry] = {}
        self._default_key: str | None = None
        self._workspace_root = workspace_root
        self._clone_lock = asyncio.Lock()

    def register(
        self,
        entry: RepoEntry,
        *,
        default: bool = False,
        aliases: tuple[str, ...] = (),
    ) -> None:
        """Register a repo. Keyed by both short name and `owner/name`."""
        full_key = f"{entry.owner}/{entry.name}"
        self._repos[full_key] = entry
        self._repos[entry.name] = entry
        for alias in aliases:
            self._repos[alias] = entry
        if default or self._default_key is None:
            self._default_key = full_key

    def default(self) -> RepoEntry | None:
        if self._default_key is None:
            return None
        return self._repos.get(self._default_key)

    def known(self) -> list[str]:
        """Return the list of registered keys (for diagnostics/error messages)."""
        return sorted(set(self._repos.keys()))

    async def resolve(self, repo_key: str | None) -> RepoEntry | None:
        """Resolve a repo arg to an entry, cloning unknown `owner/name` on demand.

        Returns None when the registry has no default and `repo_key` is empty,
        or when an unknown key cannot be auto-registered (no `owner/name` form).
        """
        if not repo_key:
            return self.default()

        if repo_key in self._repos:
            entry = self._repos[repo_key]
        elif "/" in repo_key:
            owner, name = repo_key.split("/", 1)
            owner = owner.strip()
            name = name.strip()
            if not owner or not name:
                return None
            default_entry = self.default()
            entry = RepoEntry(
                path=os.path.join(self._workspace_root, name),
                owner=owner,
                name=name,
                auth=default_entry.auth if default_entry else None,
            )
            self.register(entry)
        else:
            return None

        # Ensure the clone exists. Serialize so two concurrent tool calls
        # against a fresh repo don't race the clone.
        if not os.path.exists(os.path.join(entry.path, ".git")):
            async with self._clone_lock:
                if not os.path.exists(os.path.join(entry.path, ".git")):
                    ok = await init_repo(entry.path, entry.resolved_clone_url(), github_auth=entry.auth)
                    if not ok:
                        return None
        return entry
