"""GitHub API tools — CI/CD operations (builds, workflow status, logs)."""

from __future__ import annotations

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

    These tools handle CI/CD operations only (triggering builds, checking
    workflow status, reading logs). File read/write is handled by local_code tools.
    """

    auth = GitHubAuth(app_id, private_key, installation_id)
    repo = f"{repo_owner}/{repo_name}"
    client = httpx.AsyncClient(timeout=30.0)

    async def trigger_docker_build(args: dict[str, Any]) -> dict[str, Any]:
        """Trigger the Docker build workflow for specified services."""
        services = args.get("services", "")
        log.info("Tool: trigger_docker_build", extra={"services": services})
        try:
            resp = await client.post(
                f"{API}/repos/{repo}/actions/workflows/docker.yml/dispatches",
                headers=await auth.headers(),
                json={"ref": "main", "inputs": {"services": services}},
            )
            resp.raise_for_status()
            return mcp_result(f"Docker build triggered for: {services or 'all services'}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

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
            "trigger_docker_build",
            "Trigger a Docker image build for specified services (comma-separated, e.g. 'cortex,ears'). "
            "Leave empty to build all changed services.",
            {"services": str},
            trigger_docker_build,
        ),
        (
            "get_workflow_status",
            "Get the status of recent GitHub Actions workflow runs. "
            "Optionally filter by workflow filename (e.g. 'docker.yml', 'ci.yml').",
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
            "create_issue",
            "Create a new issue. Labels are comma-separated.",
            {"repo": str, "title": str, "body": str, "labels": str},
            create_issue,
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
    ]
