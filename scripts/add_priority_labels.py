"""One-off script to add missing priority labels to GitHub issues.

Run in stem pod: python /app/scripts/add_priority_labels.py
"""

import asyncio
import os
import sys

# Add the common package to path so we can reuse GitHubAuth
sys.path.insert(0, "/app/src")

import httpx

from maki_common.tools.github import GitHubAuth

API = "https://api.github.com"
REPO = "adhityaravi/maki"

# Issues missing priority labels and their correct priority
LABEL_MAP = {
    9: "P2",   # Care loop noise
    17: "P3",  # PR when blocked
    18: "P4",  # Write README
    19: "P2",  # Idle loop dedup
}


async def main():
    auth = GitHubAuth(
        app_id=os.environ["GITHUB_APP_ID"],
        private_key=os.environ["GITHUB_PRIVATE_KEY"],
        installation_id=os.environ["GITHUB_INSTALLATION_ID"],
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        for issue_number, priority in LABEL_MAP.items():
            # Add label to issue (POST adds without removing existing labels)
            resp = await client.post(
                f"{API}/repos/{REPO}/issues/{issue_number}/labels",
                headers=await auth.headers(),
                json={"labels": [priority]},
            )
            if resp.status_code < 300:
                print(f"✅ #{issue_number} → {priority}")
            else:
                print(f"❌ #{issue_number} → {resp.status_code}: {resp.text}")


if __name__ == "__main__":
    asyncio.run(main())
