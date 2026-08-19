"""GitHub API tools — CI/CD operations (workflow status, logs)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubAuth:
    """GitHub App authentication — generates installation tokens from JWT.

    Used by both GitHub CI tools and local git push operations.
    """

    def __init__(self, app_id: str, private_key: str, installation_id: str):
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._token: str | None = None
        self._token_expires: float = 0

    def _make_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        app_jwt = self._make_jwt()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API}/app/installations/{self._installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["token"]
            self._token_expires = time.time() + 3600
            log.info("GitHub installation token refreshed")
            return self._token

    async def headers(self) -> dict[str, str]:
        token = await self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def make_github_ci_tools(
    app_id: str,
    private_key: str,
    installation_id: str,
    repo_owner: str,
    repo_name: str,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for GitHub CI tools.

    These tools handle CI/CD operations only (checking workflow status,
    reading logs). Docker builds happen automatically via CI on push.
    """

    auth = GitHubAuth(app_id, private_key, installation_id)
    repo = f"{repo_owner}/{repo_name}"
    client = httpx.AsyncClient(timeout=30.0)

    async def get_workflow_status(args: dict[str, Any]) -> dict[str, Any]:
        """Get the status of recent workflow runs."""
        workflow = args.get("workflow", "")
        log.info("Tool: get_workflow_status", extra={"workflow": workflow})
        try:
            url = f"{API}/repos/{repo}/actions/runs"
            params: dict[str, Any] = {"per_page": 5}
            if workflow:
                url = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs"
            resp = await client.get(url, headers=await auth.headers(), params=params)
            resp.raise_for_status()
            runs = resp.json().get("workflow_runs", [])
            if not runs:
                return mcp_result("No workflow runs found.")
            lines = []
            for run in runs:
                sha = run.get("head_sha", "")[:7]
                lines.append(
                    f"#{run['run_number']} (id:{run['id']}) {run['name']} "
                    f"— {run['status']}/{run.get('conclusion', 'pending')} "
                    f"(sha: {sha}, {run['created_at']})"
                )
            return mcp_result("\n".join(lines))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def get_workflow_logs(args: dict[str, Any]) -> dict[str, Any]:
        """Get logs from a workflow run, focusing on failed jobs."""
        run_id = args.get("run_id", "")
        log.info("Tool: get_workflow_logs", extra={"run_id": run_id})
        try:
            if not run_id:
                # Find the latest failed run
                resp = await client.get(
                    f"{API}/repos/{repo}/actions/runs",
                    headers=await auth.headers(),
                    params={"per_page": 10, "status": "failure"},
                )
                resp.raise_for_status()
                runs = resp.json().get("workflow_runs", [])
                if not runs:
                    return mcp_result("No failed workflow runs found.")
                run_id = runs[0]["id"]

            # Get jobs for this run
            resp = await client.get(
                f"{API}/repos/{repo}/actions/runs/{run_id}/jobs",
                headers=await auth.headers(),
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])

            if not jobs:
                return mcp_result(f"No jobs found for run {run_id}.")

            output_parts = []
            for job in jobs:
                if job.get("conclusion") == "success":
                    continue
                # Fetch logs for non-success jobs
                log_resp = await client.get(
                    f"{API}/repos/{repo}/actions/jobs/{job['id']}/logs",
                    headers=await auth.headers(),
                    follow_redirects=True,
                )
                if log_resp.status_code == 200:
                    log_text = log_resp.text
                    # Keep last 5000 chars to stay within reasonable size
                    if len(log_text) > 5000:
                        log_text = "... (truncated, showing last 5000 chars)\n" + log_text[-5000:]
                    output_parts.append(f"=== Job: {job['name']} ({job['conclusion']}) ===\n{log_text}")
                else:
                    output_parts.append(
                        f"=== Job: {job['name']} ({job['conclusion']}) === Failed to fetch logs: {log_resp.status_code}"
                    )

            if not output_parts:
                return mcp_result(f"All jobs in run {run_id} succeeded.")

            return mcp_result("\n\n".join(output_parts))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    return [
        (
            "get_workflow_status",
            "Get the status of recent GitHub Actions CI workflow runs. "
            "Optionally filter by workflow filename (e.g. 'ci.yml').",
            {"workflow": str},
            get_workflow_status,
        ),
        (
            "get_workflow_logs",
            "Get logs from a GitHub Actions workflow run, focusing on failed jobs. "
            "Provide a run_id, or leave empty to get the latest failed run's logs.",
            {"run_id": str},
            get_workflow_logs,
        ),
    ]


def make_github_issues_tools(
    app_id: str,
    private_key: str,
    installation_id: str,
    default_owner: str,
    default_repo: str,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for GitHub Issues tools.

    All tools accept an optional 'repo' param (e.g. 'charmarr/charmarr') to
    operate on repos other than the default maki repo.
    """

    auth = GitHubAuth(app_id, private_key, installation_id)
    client = httpx.AsyncClient(timeout=30.0)

    def _resolve_repo(args: dict[str, Any]) -> str:
        repo = args.get("repo", "").strip()
        if repo:
            # Accept 'owner/repo' or just 'repo' (defaults to same owner)
            if "/" not in repo:
                return f"{default_owner}/{repo}"
            return repo
        return f"{default_owner}/{default_repo}"

    async def list_issues(args: dict[str, Any]) -> dict[str, Any]:
        """List issues for a repository."""
        repo = _resolve_repo(args)
        state = args.get("state", "open")
        labels = args.get("labels", "")
        log.info("Tool: list_issues", extra={"repo": repo, "state": state})
        try:
            params: dict[str, Any] = {"per_page": 20, "state": state}
            if labels:
                params["labels"] = labels
            resp = await client.get(
                f"{API}/repos/{repo}/issues",
                headers=await auth.headers(),
                params=params,
            )
            resp.raise_for_status()
            issues = resp.json()
            if not issues:
                return mcp_result(f"No {state} issues found in {repo}.")
            lines = []
            for issue in issues:
                # Skip pull requests (GitHub API returns them as issues too)
                if issue.get("pull_request"):
                    continue
                label_tags = ", ".join(lb["name"] for lb in issue.get("labels", []))
                label_str = f" [{label_tags}]" if label_tags else ""
                assignee = issue.get("assignee")
                assignee_str = f" @{assignee['login']}" if assignee else ""
                lines.append(f"#{issue['number']} {issue['title']}{label_str}{assignee_str} ({issue['state']})")
            if not lines:
                return mcp_result(f"No {state} issues found in {repo} (only PRs).")
            return mcp_result(f"Issues in {repo}:\n" + "\n".join(lines))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def search_issues_by_symbol(args: dict[str, Any]) -> dict[str, Any]:
        """Find open issues whose title/body mentions ≥2 of the given symbols.

        Structural dedup for the idle loop (see #682). Title-only substring
        checks miss semantic duplicates — the same bug re-derived from a
        different entry point ends up with a different title. Passing 3–5
        identifiers (function names, filenames, class names) from a draft
        catches those cases because the identifiers themselves are stable
        across different phrasings.
        """
        repo = _resolve_repo(args)
        raw_symbols = args.get("symbols", "")
        state = args.get("state", "open").strip() or "open"
        try:
            min_matches = int(args.get("min_matches", "2") or "2")
        except (TypeError, ValueError):
            min_matches = 2
        symbols = [s.strip() for s in raw_symbols.split(",") if s.strip()]
        log.info(
            "Tool: search_issues_by_symbol",
            extra={"repo": repo, "symbols": symbols, "state": state},
        )
        if not symbols:
            return mcp_result("Error: 'symbols' must be a non-empty comma-separated list.")
        # Cap at 5 terms — mirrors the GitHubIssueClient cap so the tool and
        # the client-side helper have identical semantics.
        symbols = symbols[:5]
        or_clause = " OR ".join(f'"{s}"' for s in symbols)
        search_q = f"repo:{repo} is:issue is:{state} ({or_clause})"
        try:
            resp = await client.get(
                f"{API}/search/issues",
                headers=await auth.headers(),
                params={"q": search_q, "per_page": 40},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

        lowered = [s.lower() for s in symbols]
        scored: list[dict[str, Any]] = []
        for item in items:
            if "pull_request" in item:  # /search/issues also returns PRs
                continue
            haystack = f"{item.get('title', '')}\n{item.get('body') or ''}".lower()
            matched = [s for s, low in zip(symbols, lowered, strict=True) if low in haystack]
            if len(matched) < min_matches:
                continue
            scored.append({"number": item.get("number"), "title": item.get("title", ""), "matched": matched})
        scored.sort(key=lambda r: len(r["matched"]), reverse=True)
        scored = scored[:20]

        if not scored:
            return mcp_result(
                f"No {state} issues in {repo} match ≥{min_matches} of {symbols}. "
                f"Safe to file new — no existing issue covers this symbol set."
            )
        lines = [
            f"Found {len(scored)} candidate duplicate(s) — "
            f"prefer commenting on the highest-scoring match over filing new:"
        ]
        for r in scored:
            lines.append(
                f"#{r['number']} ({len(r['matched'])}/{len(symbols)} matched: {', '.join(r['matched'])}) — {r['title']}"
            )
        return mcp_result("\n".join(lines))

    async def get_issue(args: dict[str, Any]) -> dict[str, Any]:
        """Get details and comments for a specific issue."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        log.info("Tool: get_issue", extra={"repo": repo, "number": number})
        if not number:
            return mcp_result("Error: 'number' is required.")
        try:
            resp = await client.get(
                f"{API}/repos/{repo}/issues/{number}",
                headers=await auth.headers(),
            )
            resp.raise_for_status()
            issue = resp.json()

            label_tags = ", ".join(lb["name"] for lb in issue.get("labels", []))
            assignee = issue.get("assignee")
            parts = [
                f"#{issue['number']} {issue['title']}",
                f"State: {issue['state']}",
                f"Author: @{issue['user']['login']}",
            ]
            if label_tags:
                parts.append(f"Labels: {label_tags}")
            if assignee:
                parts.append(f"Assignee: @{assignee['login']}")
            if issue.get("body"):
                parts.append(f"\n{issue['body']}")

            # Fetch comments
            if issue.get("comments", 0) > 0:
                comments_resp = await client.get(
                    f"{API}/repos/{repo}/issues/{number}/comments",
                    headers=await auth.headers(),
                    params={"per_page": 20},
                )
                if comments_resp.status_code == 200:
                    comments = comments_resp.json()
                    parts.append(f"\n--- Comments ({len(comments)}) ---")
                    for c in comments:
                        body = c["body"]
                        if len(body) > 500:
                            body = body[:500] + "..."
                        parts.append(f"@{c['user']['login']} ({c['created_at']}):\n{body}")

            return mcp_result("\n".join(parts))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def create_issue(args: dict[str, Any]) -> dict[str, Any]:
        """Create a new issue."""
        repo = _resolve_repo(args)
        title = args.get("title", "")
        body = args.get("body", "")
        labels = args.get("labels", "")
        log.info("Tool: create_issue", extra={"repo": repo, "title": title})
        if not title:
            return mcp_result("Error: 'title' is required.")
        try:
            payload: dict[str, Any] = {"title": title}
            if body:
                payload["body"] = body
            if labels:
                payload["labels"] = [lb.strip() for lb in labels.split(",")]
            resp = await client.post(
                f"{API}/repos/{repo}/issues",
                headers=await auth.headers(),
                json=payload,
            )
            resp.raise_for_status()
            issue = resp.json()
            return mcp_result(f"Created #{issue['number']}: {issue['title']}\n{issue['html_url']}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def create_pr(args: dict[str, Any]) -> dict[str, Any]:
        """Create a pull request."""
        repo = _resolve_repo(args)
        title = args.get("title", "")
        body = args.get("body", "")
        head = args.get("head", "")
        base = args.get("base", "main")
        draft = args.get("draft", "false").lower() == "true"
        reviewers = args.get("reviewers", "")
        log.info("Tool: create_pr", extra={"repo": repo, "head": head, "base": base})
        if not title or not head:
            return mcp_result("Error: 'title' and 'head' are required.")
        try:
            payload: dict[str, Any] = {"title": title, "head": head, "base": base, "draft": draft}
            if body:
                payload["body"] = body
            resp = await client.post(
                f"{API}/repos/{repo}/pulls",
                headers=await auth.headers(),
                json=payload,
            )
            resp.raise_for_status()
            pr = resp.json()
            pr_number = pr["number"]
            pr_url = pr["html_url"]

            # Request reviewers if provided
            if reviewers:
                reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()]
                await client.post(
                    f"{API}/repos/{repo}/pulls/{pr_number}/requested_reviewers",
                    headers=await auth.headers(),
                    json={"reviewers": reviewer_list},
                )

            return mcp_result(f"Created PR #{pr_number}: {title}\n{pr_url}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def list_prs(args: dict[str, Any]) -> dict[str, Any]:
        """List pull requests for a repository."""
        repo = _resolve_repo(args)
        state = args.get("state", "open")
        log.info("Tool: list_prs", extra={"repo": repo, "state": state})
        try:
            resp = await client.get(
                f"{API}/repos/{repo}/pulls",
                headers=await auth.headers(),
                params={"state": state, "per_page": 20},
            )
            resp.raise_for_status()
            prs = resp.json()
            if not prs:
                return mcp_result(f"No {state} PRs found in {repo}.")
            lines = []
            for pr in prs:
                draft_tag = " [DRAFT]" if pr.get("draft") else ""
                reviewers = ", ".join(f"@{r['login']}" for r in pr.get("requested_reviewers", []))
                reviewer_str = f" (reviewers: {reviewers})" if reviewers else ""
                lines.append(
                    f"#{pr['number']} {pr['title']}{draft_tag} "
                    f"[{pr['head']['ref']} → {pr['base']['ref']}]{reviewer_str}"
                )
            return mcp_result(f"PRs in {repo}:\n" + "\n".join(lines))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def get_pr(args: dict[str, Any]) -> dict[str, Any]:
        """Get full details of a pull request: metadata, changed files, reviews, and comments."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        log.info("Tool: get_pr", extra={"repo": repo, "number": number})
        if not number:
            return mcp_result("Error: 'number' is required.")
        try:
            headers = await auth.headers()
            # Fetch PR metadata, files, reviews, and comments in parallel
            pr_resp, files_resp, reviews_resp, comments_resp = await asyncio.gather(
                client.get(f"{API}/repos/{repo}/pulls/{number}", headers=headers),
                client.get(
                    f"{API}/repos/{repo}/pulls/{number}/files",
                    headers=headers,
                    params={"per_page": 50},
                ),
                client.get(f"{API}/repos/{repo}/pulls/{number}/reviews", headers=headers),
                client.get(
                    f"{API}/repos/{repo}/issues/{number}/comments",
                    headers=headers,
                    params={"per_page": 30},
                ),
            )
            pr_resp.raise_for_status()
            pr = pr_resp.json()

            draft_tag = " [DRAFT]" if pr.get("draft") else ""
            label_tags = ", ".join(lb["name"] for lb in pr.get("labels", []))
            parts = [
                f"PR #{pr['number']}: {pr['title']}{draft_tag}",
                f"State: {pr['state']} | {pr['head']['ref']} → {pr['base']['ref']}",
                f"Author: @{pr['user']['login']}",
            ]
            if label_tags:
                parts.append(f"Labels: {label_tags}")
            reviewers = ", ".join(f"@{r['login']}" for r in pr.get("requested_reviewers", []))
            if reviewers:
                parts.append(f"Reviewers: {reviewers}")
            if pr.get("body"):
                parts.append(f"\n{pr['body']}")

            # Changed files
            if files_resp.status_code == 200:
                files = files_resp.json()
                if files:
                    parts.append(f"\n--- Changed files ({len(files)}) ---")
                    for f in files[:30]:
                        parts.append(f"  {f['status']} +{f['additions']}/-{f['deletions']} {f['filename']}")

            # Reviews
            if reviews_resp.status_code == 200:
                reviews = reviews_resp.json()
                if reviews:
                    parts.append(f"\n--- Reviews ({len(reviews)}) ---")
                    for r in reviews:
                        state_str = r.get("state", "COMMENTED")
                        body = r.get("body", "")
                        body_str = f": {body[:200]}" if body else ""
                        parts.append(f"  @{r['user']['login']} — {state_str}{body_str}")

            # General comments
            if comments_resp.status_code == 200:
                comments = comments_resp.json()
                if comments:
                    parts.append(f"\n--- Comments ({len(comments)}) ---")
                    for c in comments:
                        body = c["body"]
                        if len(body) > 400:
                            body = body[:400] + "..."
                        parts.append(f"@{c['user']['login']} ({c['created_at']}):\n{body}")

            return mcp_result("\n".join(parts))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def comment_pr(args: dict[str, Any]) -> dict[str, Any]:
        """Add a general comment to a pull request."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        body = args.get("body", "")
        log.info("Tool: comment_pr", extra={"repo": repo, "number": number})
        if not number or not body:
            return mcp_result("Error: 'number' and 'body' are required.")
        try:
            # PR comments use the issues endpoint
            resp = await client.post(
                f"{API}/repos/{repo}/issues/{number}/comments",
                headers=await auth.headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            comment = resp.json()
            return mcp_result(f"Comment added to PR #{number}: {comment['html_url']}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def update_pr(args: dict[str, Any]) -> dict[str, Any]:
        """Update a pull request's title, body, or base branch."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        log.info("Tool: update_pr", extra={"repo": repo, "number": number})
        if not number:
            return mcp_result("Error: 'number' is required.")
        try:
            payload: dict[str, Any] = {}
            if args.get("title"):
                payload["title"] = args["title"]
            if args.get("body"):
                payload["body"] = args["body"]
            if args.get("base"):
                payload["base"] = args["base"]
            if args.get("draft"):
                payload["draft"] = args["draft"].lower() == "true"
            if not payload:
                return mcp_result("Error: provide at least one of 'title', 'body', 'base', or 'draft' to update.")
            resp = await client.patch(
                f"{API}/repos/{repo}/pulls/{number}",
                headers=await auth.headers(),
                json=payload,
            )
            resp.raise_for_status()
            pr = resp.json()
            return mcp_result(f"Updated PR #{number}: {pr['title']}\n{pr['html_url']}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def merge_pr(args: dict[str, Any]) -> dict[str, Any]:
        """Merge a pull request. Method: 'squash' (default), 'merge', or 'rebase'."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        method = args.get("method", "squash")
        commit_title = args.get("commit_title", "")
        commit_message = args.get("commit_message", "")
        log.info("Tool: merge_pr", extra={"repo": repo, "number": number, "method": method})
        if not number:
            return mcp_result("Error: 'number' is required.")
        if method not in ("squash", "merge", "rebase"):
            return mcp_result("Error: 'method' must be 'squash', 'merge', or 'rebase'.")
        try:
            payload: dict[str, Any] = {"merge_method": method}
            if commit_title:
                payload["commit_title"] = commit_title
            if commit_message:
                payload["commit_message"] = commit_message
            resp = await client.put(
                f"{API}/repos/{repo}/pulls/{number}/merge",
                headers=await auth.headers(),
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            sha = result.get("sha", "")[:7]
            msg = result.get("message", "success")
            return mcp_result(f"Merged PR #{number}: {msg} (sha: {sha})")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def close_pr(args: dict[str, Any]) -> dict[str, Any]:
        """Close a pull request without merging. Optionally add a comment."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        comment = args.get("comment", "")
        log.info("Tool: close_pr", extra={"repo": repo, "number": number})
        if not number:
            return mcp_result("Error: 'number' is required.")
        try:
            if comment:
                await client.post(
                    f"{API}/repos/{repo}/issues/{number}/comments",
                    headers=await auth.headers(),
                    json={"body": comment},
                )
            resp = await client.patch(
                f"{API}/repos/{repo}/pulls/{number}",
                headers=await auth.headers(),
                json={"state": "closed"},
            )
            resp.raise_for_status()
            return mcp_result(f"Closed PR #{number} in {repo} without merging.")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def request_pr_review(args: dict[str, Any]) -> dict[str, Any]:
        """Request reviewers for a pull request."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        reviewers = args.get("reviewers", "")
        log.info("Tool: request_pr_review", extra={"repo": repo, "number": number})
        if not number or not reviewers:
            return mcp_result("Error: 'number' and 'reviewers' are required.")
        try:
            reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()]
            resp = await client.post(
                f"{API}/repos/{repo}/pulls/{number}/requested_reviewers",
                headers=await auth.headers(),
                json={"reviewers": reviewer_list},
            )
            resp.raise_for_status()
            return mcp_result(f"Requested review from {', '.join(reviewer_list)} on PR #{number}.")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def close_issue(args: dict[str, Any]) -> dict[str, Any]:
        """Close an issue."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        comment = args.get("comment", "")
        log.info("Tool: close_issue", extra={"repo": repo, "number": number})
        if not number:
            return mcp_result("Error: 'number' is required.")
        try:
            # Add closing comment if provided
            if comment:
                await client.post(
                    f"{API}/repos/{repo}/issues/{number}/comments",
                    headers=await auth.headers(),
                    json={"body": comment},
                )
            resp = await client.patch(
                f"{API}/repos/{repo}/issues/{number}",
                headers=await auth.headers(),
                json={"state": "closed"},
            )
            resp.raise_for_status()
            return mcp_result(f"Closed #{number} in {repo}.")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def comment_issue(args: dict[str, Any]) -> dict[str, Any]:
        """Add a comment to an issue."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        body = args.get("body", "")
        log.info("Tool: comment_issue", extra={"repo": repo, "number": number})
        if not number or not body:
            return mcp_result("Error: 'number' and 'body' are required.")
        try:
            resp = await client.post(
                f"{API}/repos/{repo}/issues/{number}/comments",
                headers=await auth.headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            comment = resp.json()
            return mcp_result(f"Comment added to #{number}: {comment['html_url']}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def add_label(args: dict[str, Any]) -> dict[str, Any]:
        """Add a label to an issue."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        label = args.get("label", "")
        log.info("Tool: add_label", extra={"repo": repo, "number": number, "label": label})
        if not number or not label:
            return mcp_result("Error: 'number' and 'label' are required.")
        try:
            resp = await client.post(
                f"{API}/repos/{repo}/issues/{number}/labels",
                headers=await auth.headers(),
                json={"labels": [label]},
            )
            resp.raise_for_status()
            return mcp_result(f"Label '{label}' added to #{number}.")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def remove_label(args: dict[str, Any]) -> dict[str, Any]:
        """Remove a label from an issue."""
        repo = _resolve_repo(args)
        number = args.get("number", "")
        label = args.get("label", "")
        log.info("Tool: remove_label", extra={"repo": repo, "number": number, "label": label})
        if not number or not label:
            return mcp_result("Error: 'number' and 'label' are required.")
        try:
            resp = await client.delete(
                f"{API}/repos/{repo}/issues/{number}/labels/{label}",
                headers=await auth.headers(),
            )
            resp.raise_for_status()
            return mcp_result(f"Label '{label}' removed from #{number}.")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    return [
        (
            "list_issues",
            "List issues for a GitHub repo. Defaults to maki repo. "
            "Use 'repo' param for other repos (e.g. 'charmarr/charmarr', 'charmarr/charmarr-lib').",
            {"repo": str, "state": str, "labels": str},
            list_issues,
        ),
        (
            "get_issue",
            "Get details and comments for a specific issue by number.",
            {"repo": str, "number": str},
            get_issue,
        ),
        (
            "search_issues_by_symbol",
            "Find open issues whose title/body mentions ≥2 of the given identifiers. "
            "Structural dedup for the idle loop — pass 3–5 symbols (function names, "
            "filenames, class names) extracted from a draft body; matches identify "
            "existing issues covering the same underlying bug even when titles differ. "
            "Params: symbols (comma-separated), state ('open'/'closed', default 'open'), "
            "min_matches (default 2). Prefer commenting on the highest-scoring match "
            "over filing new.",
            {"repo": str, "symbols": str, "state": str, "min_matches": str},
            search_issues_by_symbol,
        ),
        (
            "create_issue",
            "Create a new issue. Labels are comma-separated.",
            {"repo": str, "title": str, "body": str, "labels": str},
            create_issue,
        ),
        (
            "create_pr",
            "Create a pull request. 'head' is the source branch, 'base' defaults to 'main'. "
            "Optionally set 'draft' to 'true' and provide comma-separated 'reviewers'.",
            {"repo": str, "title": str, "body": str, "head": str, "base": str, "draft": str, "reviewers": str},
            create_pr,
        ),
        (
            "list_prs",
            "List pull requests for a repo. State: 'open' (default), 'closed', or 'all'.",
            {"repo": str, "state": str},
            list_prs,
        ),
        (
            "get_pr",
            "Get full details of a PR: metadata, changed files, reviews, and comments.",
            {"repo": str, "number": str},
            get_pr,
        ),
        (
            "comment_pr",
            "Add a general comment to a pull request.",
            {"repo": str, "number": str, "body": str},
            comment_pr,
        ),
        (
            "update_pr",
            "Update a PR's title, body, base branch, or draft status.",
            {"repo": str, "number": str, "title": str, "body": str, "base": str, "draft": str},
            update_pr,
        ),
        (
            "merge_pr",
            "Merge a pull request. Method: 'squash' (default), 'merge', or 'rebase'. "
            "Optionally set 'commit_title' and 'commit_message'.",
            {"repo": str, "number": str, "method": str, "commit_title": str, "commit_message": str},
            merge_pr,
        ),
        (
            "close_pr",
            "Close a pull request without merging. Optionally add a closing comment.",
            {"repo": str, "number": str, "comment": str},
            close_pr,
        ),
        (
            "request_pr_review",
            "Request reviewers for a pull request. Reviewers are comma-separated GitHub usernames.",
            {"repo": str, "number": str, "reviewers": str},
            request_pr_review,
        ),
        (
            "close_issue",
            "Close an issue, optionally with a closing comment.",
            {"repo": str, "number": str, "comment": str},
            close_issue,
        ),
        (
            "comment_issue",
            "Add a comment to an issue.",
            {"repo": str, "number": str, "body": str},
            comment_issue,
        ),
        (
            "add_label",
            "Add a label to an issue. Use this to apply labels like 'human', 'P1', etc.",
            {"repo": str, "number": str, "label": str},
            add_label,
        ),
        (
            "remove_label",
            "Remove a label from an issue. Use this to undo a label you added in error.",
            {"repo": str, "number": str, "label": str},
            remove_label,
        ),
    ]
