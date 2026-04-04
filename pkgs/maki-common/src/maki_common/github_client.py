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

# Priority label ordering for work loop task selection.
PRIORITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}


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

    async def list_issues(
        self,
        state: str = "open",
        labels: str = "",
        per_page: int = 30,
    ) -> list[dict[str, Any]]:
        """List issues from the repo, optionally filtered by state and labels.

        Returns issues sorted by priority label (P1 first). Issues without
        a priority label are sorted last.
        """
        try:
            params: dict[str, Any] = {
                "state": state,
                "per_page": per_page,
                "sort": "created",
                "direction": "asc",
            }
            if labels:
                params["labels"] = labels

            resp = await self._client.get(
                f"{API}/repos/{self._repo_path}/issues",
                headers=await self._auth.headers(),
                params=params,
            )
            resp.raise_for_status()
            issues = resp.json()

            # Filter out pull requests (GitHub API returns PRs as issues too)
            issues = [i for i in issues if "pull_request" not in i]

            # Sort by priority label
            def _priority_key(issue: dict[str, Any]) -> int:
                for label in issue.get("labels", []):
                    name = label.get("name", "") if isinstance(label, dict) else str(label)
                    if name in PRIORITY_ORDER:
                        return PRIORITY_ORDER[name]
                return 99  # No priority label → lowest

            issues.sort(key=_priority_key)

            log.info(
                "Listed GitHub issues",
                extra={"count": len(issues), "state": state},
            )
            return issues
        except Exception:
            log.exception("Failed to list GitHub issues")
            return []

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

    async def get_issue_comments(self, number: int, per_page: int = 50) -> list[dict[str, Any]]:
        """Fetch all comments on an issue. Returns list of comment dicts with 'author' and 'body'."""
        try:
            resp = await self._client.get(
                f"{API}/repos/{self._repo_path}/issues/{number}/comments",
                headers=await self._auth.headers(),
                params={"per_page": per_page},
            )
            resp.raise_for_status()
            raw = resp.json()
            comments = [
                {
                    "author": c.get("user", {}).get("login", "unknown"),
                    "body": c.get("body", ""),
                    "created_at": c.get("created_at", ""),
                }
                for c in raw
            ]
            log.info("GitHub issue comments fetched", extra={"number": number, "count": len(comments)})
            return comments
        except Exception:
            log.exception("Failed to fetch issue comments", extra={"number": number})
            return []

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
