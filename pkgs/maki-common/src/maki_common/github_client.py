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

    async def _request(
        self,
        method: str,
        url: str,
        *,
        err_log: str,
        err_extra: dict[str, Any] | None = None,
        ok_log: str | None = None,
        ok_extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response | None:
        """Shared boilerplate for GitHub API calls.

        Every public method repeats the same shape: attach GitHub App auth
        headers, fire the request, ``raise_for_status``, log success or
        exception, and return a typed default on failure. This helper collapses
        that boilerplate into one place so future cross-cutting changes (retry,
        rate-limit, metrics) land in a single spot.

        - On success: returns the ``httpx.Response``. If ``ok_log`` is given,
          emits an info log with ``ok_extra`` before returning — callers that
          need response-dependent fields (e.g. the created issue number) should
          omit ``ok_log`` and log themselves after post-processing.
        - On failure: emits an exception log with ``err_extra`` and returns
          ``None``. Callers translate ``None`` into their own typed default
          (empty list, ``False``, ``None``, etc.).
        """
        try:
            resp = await self._client.request(
                method,
                url,
                headers=await self._auth.headers(),
                **kwargs,
            )
            resp.raise_for_status()
            if ok_log is not None:
                log.info(ok_log, extra=ok_extra or {})
            return resp
        except Exception:
            log.exception(err_log, extra=err_extra or {})
            return None

    async def list_issues(
        self,
        state: str = "open",
        labels: str = "",
        max_results: int | None = 200,
    ) -> list[dict[str, Any]]:
        """List issues from the repo, optionally filtered by state and labels.

        Paginates through all results so issues beyond the first 30 are not
        silently dropped. Returns issues sorted by priority label (P1 first).
        Issues without a priority label are sorted last.

        ``max_results`` behavior:
        - ``None`` — no cap; page until GitHub returns the last page. This is
          the canonical "all open" view stem's loops need — with 250+ open
          issues, a cap silently starves the work loop of newly-filed P1/P2s.
        - ``int`` (default 200) — priority sort is applied to the *full*
          fetched set *before* the cap, so the tail dropped is always the
          lowest-priority issues, never the newest ones. Note the cap only
          bounds the returned slice; pagination still fetches every issue up
          to the last page hit.

        Fetch direction is ``desc`` (newest first) so that within each
        priority tier — and especially the untriaged (99) tier that holds
        most issues — the head of the returned list is the *newest*
        content. This matters because the priority sort is stable: within
        a tier, items keep their fetch order. If a caller hits ``max_results``
        and truncation drops the tail, dropping the *oldest* untriaged is
        vastly safer than dropping the *newest* — the reflection dedup
        pass only works if it can see what was recently filed. See #552.
        """
        try:
            issues: list[dict[str, Any]] = []
            page = 1

            # Always page until GitHub returns a short page — the cap only
            # bounds the returned slice, not the fetch. Breaking early on
            # ``len(issues) >= max_results`` would defeat priority-sort-
            # before-truncate: a P1 living on the next page would never be
            # fetched and would never land at the head of the returned list
            # (see #404 for the original sort-then-truncate fix). With 250+
            # open issues this means ~3 API calls per invocation, which is
            # cheap relative to the correctness win.
            while True:
                params: dict[str, Any] = {
                    "state": state,
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "desc",
                }
                if labels:
                    params["labels"] = labels

                resp = await self._request(
                    "GET",
                    f"{API}/repos/{self._repo_path}/issues",
                    err_log="Failed to list GitHub issues",
                    err_extra={"state": state, "page": page},
                    params=params,
                )
                if resp is None:
                    return []
                raw = resp.json()
                raw_count = len(raw)

                # Filter out pull requests (GitHub API returns PRs as issues too)
                batch = [i for i in raw if "pull_request" not in i]

                issues.extend(batch)

                # If we got fewer than a full page, we've exhausted the results.
                # (Check raw_count, not len(batch): a page that's all PRs still
                # means there may be more pages behind it — see issue #248.)
                if raw_count < 100:
                    break

                page += 1

            # Sort by priority label across the full result set BEFORE truncating.
            # Sorting-then-truncating protects newly-filed P1/P2 issues when the
            # open-issue count exceeds max_results: they land at the tail of the
            # asc-by-created fetch, but their priority pulls them to the head of
            # the returned list. Truncate-then-sort would silently drop them —
            # exactly the bug this method used to have.
            def _priority_key(issue: dict[str, Any]) -> int:
                for label in issue.get("labels", []):
                    name = label.get("name", "") if isinstance(label, dict) else str(label)
                    if name in PRIORITY_ORDER:
                        return PRIORITY_ORDER[name]
                return 99  # No priority label → lowest

            issues.sort(key=_priority_key)

            if max_results is not None:
                issues = issues[:max_results]

            log.info(
                "Listed GitHub issues",
                extra={"count": len(issues), "state": state, "pages": page},
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
        search_q = f'repo:{self._repo_path} is:issue is:open "{title_query}" in:title'
        resp = await self._request(
            "GET",
            f"{API}/search/issues",
            err_log="Failed to search GitHub issues",
            params={"q": search_q, "per_page": 5},
        )
        if resp is None:
            return None
        try:
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

    async def search_issues_by_symbols(
        self,
        symbols: list[str],
        *,
        state: str = "open",
        min_matches: int = 2,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Find issues whose title/body mentions ≥``min_matches`` of ``symbols``.

        The idle-loop dedup rule (title-only substring match on a truncated
        dashboard slice) misses semantic duplicates: the same underlying bug
        gets re-derived from a different entry point, phrased around that
        entry point, and filed as a new issue. See #682 for the systemic map.

        This method is the structural fix. Callers extract 3–5 identifiers
        (functions, filenames, class names) from a draft body and ask: "does
        any open issue *already* discuss ≥2 of these symbols?" If yes, the
        caller should comment on the existing issue instead of filing new.

        Symbols are quoted in the GitHub search query so identifiers with
        underscores/dots (``get_issue_comments``, ``github_client.py``) match
        as whole tokens rather than word-broken fragments. Matches are then
        re-counted locally against title+body because GitHub's ranking
        doesn't tell us *which* symbols hit — we need the count to enforce
        the ``min_matches`` threshold.

        Args:
            symbols: 1–5 identifiers (function/class/file names). Empty or
                whitespace-only entries are dropped. GitHub caps a single
                search at 256 chars, so callers should pass tight symbol
                sets, not sprawling n-grams.
            state: ``"open"`` (default) or ``"closed"``.
            min_matches: Return only issues where this many symbols hit.
                Default 2 — one shared identifier is too weak (many issues
                mention ``request_restart`` incidentally), two is strong.
            max_results: Cap the returned list. Default 20 keeps the dedup
                pass cheap; raise only when scanning a wider net.

        Returns:
            List of ``{"number", "title", "matched": [symbols], "score": int}``,
            sorted by score desc. Empty on error, empty input, or no hits.
        """
        clean = [s.strip() for s in symbols if s and s.strip()]
        if not clean:
            return []
        # Cap the OR clause at 5 terms — GitHub search rejects longer boolean
        # queries with a 422, and the top-5 most-specific identifiers from a
        # draft are more than enough signal for dedup.
        clean = clean[:5]
        or_clause = " OR ".join(f'"{s}"' for s in clean)
        search_q = f"repo:{self._repo_path} is:issue is:{state} ({or_clause})"
        resp = await self._request(
            "GET",
            f"{API}/search/issues",
            err_log="Failed to search issues by symbols",
            err_extra={"symbols": clean},
            params={"q": search_q, "per_page": max_results * 2},
        )
        if resp is None:
            return []
        try:
            items = resp.json().get("items", [])
        except Exception:
            log.exception("Failed to parse issue-search response")
            return []

        lowered = [s.lower() for s in clean]
        scored: list[dict[str, Any]] = []
        for item in items:
            # PRs also come back from /search/issues; skip them so dedup
            # doesn't accidentally comment on an in-flight PR thread.
            if "pull_request" in item:
                continue
            haystack = f"{item.get('title', '')}\n{item.get('body') or ''}".lower()
            matched = [orig for orig, low in zip(clean, lowered, strict=True) if low in haystack]
            if len(matched) < min_matches:
                continue
            scored.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title", ""),
                    "matched": matched,
                    "score": len(matched),
                }
            )
        scored.sort(key=lambda r: r["score"], reverse=True)
        result = scored[:max_results]
        log.info(
            "Symbol issue search complete",
            extra={"symbols": clean, "hits": len(result), "state": state},
        )
        return result

    async def create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> int | None:
        """Create an issue and return the issue number, or None on failure."""
        payload: dict[str, Any] = {"title": title}
        if body:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        resp = await self._request(
            "POST",
            f"{API}/repos/{self._repo_path}/issues",
            err_log="Failed to create GitHub issue",
            err_extra={"title": title},
            json=payload,
        )
        if resp is None:
            return None
        issue = resp.json()
        log.info(
            "GitHub issue created",
            extra={"number": issue["number"], "title": title},
        )
        return issue["number"]

    async def get_issue_comments(self, number: int, per_page: int = 50) -> list[dict[str, Any]]:
        """Fetch all comments on an issue. Returns list of comment dicts with 'author' and 'body'."""
        resp = await self._request(
            "GET",
            f"{API}/repos/{self._repo_path}/issues/{number}/comments",
            err_log="Failed to fetch issue comments",
            err_extra={"number": number},
            params={"per_page": per_page},
        )
        if resp is None:
            return []
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

    async def comment_issue(self, number: int, body: str) -> bool:
        """Add a comment to an issue. Returns True on success."""
        resp = await self._request(
            "POST",
            f"{API}/repos/{self._repo_path}/issues/{number}/comments",
            ok_log="GitHub issue comment added",
            ok_extra={"number": number},
            err_log="Failed to comment on issue",
            err_extra={"number": number},
            json={"body": body},
        )
        return resp is not None

    async def get_issue(self, number: int) -> dict[str, Any] | None:
        """Fetch a single issue by number. Returns the issue dict or None on failure."""
        resp = await self._request(
            "GET",
            f"{API}/repos/{self._repo_path}/issues/{number}",
            err_log="Failed to fetch issue",
            err_extra={"number": number},
        )
        return resp.json() if resp is not None else None

    async def close_issue(self, number: int, comment: str = "") -> bool:
        """Close an issue, optionally with a closing comment. Returns True on success."""
        if comment:
            await self.comment_issue(number, comment)
        resp = await self._request(
            "PATCH",
            f"{API}/repos/{self._repo_path}/issues/{number}",
            ok_log="GitHub issue closed",
            ok_extra={"number": number},
            err_log="Failed to close issue",
            err_extra={"number": number},
            json={"state": "closed"},
        )
        return resp is not None

    async def add_label(self, number: int, label: str) -> bool:
        """Add a label to an issue. Returns True on success."""
        resp = await self._request(
            "POST",
            f"{API}/repos/{self._repo_path}/issues/{number}/labels",
            ok_log="GitHub issue label added",
            ok_extra={"number": number, "label": label},
            err_log="Failed to add label to issue",
            err_extra={"number": number, "label": label},
            json={"labels": [label]},
        )
        return resp is not None

    async def remove_label(self, number: int, label: str) -> bool:
        """Remove a label from an issue. Returns True on success."""
        resp = await self._request(
            "DELETE",
            f"{API}/repos/{self._repo_path}/issues/{number}/labels/{label}",
            ok_log="GitHub issue label removed",
            ok_extra={"number": number, "label": label},
            err_log="Failed to remove label from issue",
            err_extra={"number": number, "label": label},
        )
        return resp is not None
