"""GitHub API tools — read, write, and manage the Maki repo."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx
import jwt

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

API = "https://api.github.com"


class _GitHubAuth:
    """GitHub App authentication — generates installation tokens from JWT."""

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


def make_github_tools(
    app_id: str,
    private_key: str,
    installation_id: str,
    repo_owner: str,
    repo_name: str,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for GitHub tools."""

    auth = _GitHubAuth(app_id, private_key, installation_id)
    repo = f"{repo_owner}/{repo_name}"
    client = httpx.AsyncClient(timeout=30.0)

    def _normalize_path(path: str) -> str:
        """Normalize path for GitHub Contents API (root = empty string)."""
        return path.strip("/")

    async def get_file_content(args: dict[str, Any]) -> dict[str, Any]:
        """Read a file from the repository."""
        path = _normalize_path(args.get("path", ""))
        ref = args.get("ref", "main")
        log.info("Tool: get_file_content", extra={"path": path, "ref": ref})
        try:
            resp = await client.get(
                f"{API}/repos/{repo}/contents/{path}",
                headers=await auth.headers(),
                params={"ref": ref},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("type") != "file":
                return mcp_result(f"'{path}' is a {data.get('type')}, not a file. Use list_directory instead.")
            content = base64.b64decode(data["content"]).decode()
            return mcp_result(content)
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def list_directory(args: dict[str, Any]) -> dict[str, Any]:
        """List contents of a directory in the repository."""
        path = _normalize_path(args.get("path", ""))
        ref = args.get("ref", "main")
        log.info("Tool: list_directory", extra={"path": path, "ref": ref})
        try:
            resp = await client.get(
                f"{API}/repos/{repo}/contents/{path}",
                headers=await auth.headers(),
                params={"ref": ref},
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return mcp_result(f"'{path}' is a file, not a directory. Use get_file_content instead.")
            lines = [f"{'d' if item['type'] == 'dir' else 'f'}  {item['name']}" for item in data]
            return mcp_result("\n".join(lines))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def search_code(args: dict[str, Any]) -> dict[str, Any]:
        """Search code in the repository."""
        query = args.get("query", "")
        log.info("Tool: search_code", extra={"query": query})
        try:
            resp = await client.get(
                f"{API}/search/code",
                headers=await auth.headers(),
                params={"q": f"{query} repo:{repo}"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])[:20]
            if not items:
                return mcp_result("No results found.")
            lines = [f"{item['path']} (score: {item.get('score', '?')})" for item in items]
            return mcp_result(f"Found {data['total_count']} results:\n" + "\n".join(lines))
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

    async def create_or_update_file(args: dict[str, Any]) -> dict[str, Any]:
        """Create or update a file in the repository on main branch."""
        path = args.get("path", "")
        content = args.get("content", "")
        commit_msg = args.get("message", f"Update {path}")
        log.info("Tool: create_or_update_file", extra={"path": path, "commit_msg": commit_msg})
        try:
            # Get current file SHA if it exists (needed for updates)
            sha = None
            resp = await client.get(
                f"{API}/repos/{repo}/contents/{path}",
                headers=await auth.headers(),
                params={"ref": "main"},
            )
            if resp.status_code == 200:
                sha = resp.json().get("sha")

            body: dict[str, Any] = {
                "message": commit_msg,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": "main",
            }
            if sha:
                body["sha"] = sha

            resp = await client.put(
                f"{API}/repos/{repo}/contents/{path}",
                headers=await auth.headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            commit_sha = data["commit"]["sha"][:7]
            action = "Updated" if sha else "Created"
            return mcp_result(f"{action} {path} — commit {commit_sha}")
        except httpx.HTTPStatusError as e:
            return mcp_result(f"Error: {e.response.status_code} — {e.response.text[:500]}")
        except Exception as e:
            return mcp_result(f"Error: {e}")

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
                        f"=== Job: {job['name']} ({job['conclusion']}) === "
                        f"Failed to fetch logs: {log_resp.status_code}"
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
            "get_file_content",
            "Read a file from the Maki GitHub repository.",
            {"path": str, "ref": str},
            get_file_content,
        ),
        (
            "list_directory",
            "List contents of a directory in the Maki GitHub repository.",
            {"path": str, "ref": str},
            list_directory,
        ),
        (
            "search_code",
            "Search for code patterns in the Maki GitHub repository.",
            {"query": str},
            search_code,
        ),
        (
            "create_or_update_file",
            "Create or update a file in the Maki GitHub repository on the main branch. "
            "Provide the full file content, not a diff.",
            {"path": str, "content": str, "message": str},
            create_or_update_file,
        ),
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
