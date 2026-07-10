"""Optional GitHub issue client bootstrap.

Reads env vars for App ID, private key path, installation ID and returns
a :class:`GitHubIssueClient` — or ``None`` when creds aren't configured
(issue tracking is opt-in). Kept isolated so main.py doesn't need to
import ``maki_common.github_client`` at module load time.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def init_github_client():
    """Initialize a GitHub issue client from env vars, or return None.

    Required env: ``GITHUB_APP_ID``, ``GITHUB_PRIVATE_KEY_PATH``,
    ``GITHUB_INSTALLATION_ID``. Optional: ``REPO_OWNER`` (default
    ``adhityaravi``), ``REPO_NAME`` (default ``maki``).
    """
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key_path = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
    installation_id = os.environ.get("GITHUB_INSTALLATION_ID")
    repo_owner = os.environ.get("REPO_OWNER", "adhityaravi")
    repo_name = os.environ.get("REPO_NAME", "maki")

    if not (app_id and private_key_path and installation_id):
        log.info("GitHub credentials not configured — issue tracking disabled")
        return None

    try:
        with open(private_key_path) as f:
            private_key = f.read()
    except Exception:
        log.exception("Failed to read GitHub private key")
        return None

    from maki_common.github_client import GitHubIssueClient

    client = GitHubIssueClient(
        app_id=app_id,
        private_key=private_key,
        installation_id=installation_id,
        default_owner=repo_owner,
        default_repo=repo_name,
    )
    log.info("GitHub issue client initialized")
    return client
