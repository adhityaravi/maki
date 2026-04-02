"""Lightweight GitHub issue client for stem's idle/work loops.

Reuses GitHubAuth from the MCP tools layer for GitHub App authentication.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from maki_common.tools.github import GitHubAuth

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubIssueClient:
    """Async GitHub issue client for creating, commenting, and closing issues.

    Used by stem's idle loop (create thought issues) and work loop
    (create/comment/close task issues).
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
        self._owner = default_owner
        self._repo = default_repo
        self._client = httpx.AsyncClient(timeout=30.0)

    @property
    def _repo_path(self) -> str:
        return f"{self._owner}/{self._repo}"

    async def find_open_issue(self, title_query: str) -> int | None:
        """Search for an open issue whose title contains the query string.

        Returns the issue number if found, None otherwise.
        Used to avoid creating duplicate issues for the same todo.
        """
        try:
            # GitHub search API: search in repo, open issues only
            search_q = f'repo:{self._repo_path} is:issue is:open "{title_query}" in:title'
            resp = await self._client.get(
                f"{API}/search/issues",
                headers=await self._auth.headers(),
                params={"q": search_q, "per_page": 5},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])

            # Find exact or close title match
            for item in items:
                if title_query.lower() in item["title"].lower():
                    log.info(
                        "Found existing issue",
                        extra={"number": item["number"], "title": item["title"]},
                    )
                    return item["number"]

            return None
        except Exception:
            log.exception("Failed to search GitHub issues")
            return None

    async def create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> int | None:
        """Create an issue and return the issue number, or None on failure."""
        try:
            payload: dict[str, Any] = {"title": title}
            if body:
                payload["body"] = body
            if labels:
                payload["labels"] = labels
            resp = await self._client.post(
                f"{API}/repos/{self._repo_path}/issues",
                headers=await self._auth.headers(),
                json=payload,
            )
            resp.raise_for_status()
            issue = resp.json()
            log.info(
                "GitHub issue created",
                extra={"number": issue["number"], "title": title},
            )
            return issue["number"]
        except Exception:
            log.exception("Failed to create GitHub issue", extra={"title": title})
            return None

    async def comment_issue(self, number: int, body: str) -> bool:
        """Add a comment to an issue. Returns True on success."""
        try:
            resp = await self._client.post(
                f"{API}/repos/{self._repo_path}/issues/{number}/comments",
                headers=await self._auth.headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            log.info("GitHub issue comment added", extra={"number": number})
            return True
        except Exception:
            log.exception("Failed to comment on issue", extra={"number": number})
            return False

    async def close_issue(self, number: int, comment: str = "") -> bool:
        """Close an issue, optionally with a closing comment. Returns True on success."""
        try:
            if comment:
                await self.comment_issue(number, comment)
            resp = await self._client.patch(
                f"{API}/repos/{self._repo_path}/issues/{number}",
                headers=await self._auth.headers(),
                json={"state": "closed"},
            )
            resp.raise_for_status()
            log.info("GitHub issue closed", extra={"number": number})
            return True
        except Exception:
            log.exception("Failed to close issue", extra={"number": number})
            return False
