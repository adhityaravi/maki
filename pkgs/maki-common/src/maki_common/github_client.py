"""Lightweight GitHub Issues client for use by any Maki component.

Wraps GitHubAuth and provides simple async methods for issue operations.
Gracefully degrades (logs + returns None) when credentials are missing.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from maki_common.tools.github import GitHubAuth

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubIssueClient:
    """Async client for GitHub Issues API operations.

    Args:
        app_id: GitHub App ID.
        private_key: GitHub App private key PEM string.
        installation_id: GitHub App installation ID.
        default_owner: Default repo owner (e.g. 'adhityaravi').
        default_repo: Default repo name (e.g. 'maki').
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
        default_owner: str,
        default_repo: str,
    ):
        self._auth = GitHubAuth(app_id, private_key, installation_id)
        self._default_repo = f"{default_owner}/{default_repo}"
        self._client = httpx.AsyncClient(timeout=30.0)

    def _resolve_repo(self, repo: str | None) -> str:
        if not repo:
            return self._default_repo
        return repo if "/" in repo else f"{self._default_repo.split('/')[0]}/{repo}"

    async def create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        repo: str | None = None,
    ) -> int | None:
        """Create an issue. Returns issue number or None on failure."""
        resolved = self._resolve_repo(repo)
        try:
            payload: dict[str, Any] = {"title": title}
            if body:
                payload["body"] = body
            if labels:
                payload["labels"] = labels
            resp = await self._client.post(
                f"{API}/repos/{resolved}/issues",
                headers=await self._auth.headers(),
                json=payload,
            )
            resp.raise_for_status()
            issue = resp.json()
            number = issue["number"]
            log.info(
                "GitHub issue created",
                extra={"repo": resolved, "number": number, "title": title},
            )
            return number
        except Exception:
            log.exception("Failed to create GitHub issue", extra={"repo": resolved, "title": title})
            return None

    async def comment_issue(
        self,
        number: int,
        body: str,
        repo: str | None = None,
    ) -> bool:
        """Add a comment to an issue. Returns True on success."""
        resolved = self._resolve_repo(repo)
        try:
            resp = await self._client.post(
                f"{API}/repos/{resolved}/issues/{number}/comments",
                headers=await self._auth.headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            log.info("GitHub issue commented", extra={"repo": resolved, "number": number})
            return True
        except Exception:
            log.exception("Failed to comment on GitHub issue", extra={"repo": resolved, "number": number})
            return False

    async def close_issue(
        self,
        number: int,
        comment: str = "",
        repo: str | None = None,
    ) -> bool:
        """Close an issue, optionally with a closing comment. Returns True on success."""
        resolved = self._resolve_repo(repo)
        try:
            if comment:
                await self.comment_issue(number, comment, repo=resolved)
            resp = await self._client.patch(
                f"{API}/repos/{resolved}/issues/{number}",
                headers=await self._auth.headers(),
                json={"state": "closed"},
            )
            resp.raise_for_status()
            log.info("GitHub issue closed", extra={"repo": resolved, "number": number})
            return True
        except Exception:
            log.exception("Failed to close GitHub issue", extra={"repo": resolved, "number": number})
            return False
