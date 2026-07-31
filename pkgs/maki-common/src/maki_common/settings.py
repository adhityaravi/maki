"""Central resolved env-var defaults for cross-service infrastructure.

Every service used to re-declare the same NATS URL, service URLs, and repo
identity in its own ``main.py``. When a default needed to change (nerve
cluster relocation, port standardization, org rename) it meant a 4-to-6
file edit that was routinely half-done and drifted. See issue #162.

Values are resolved once at import — process-wide constants. Service-local
env (turn timeouts, max turns, health ports, per-service tunables) stays
in the owning module.

Companion modules:

* :mod:`maki_common.db` — Postgres DSN (issue #157).
* :mod:`maki_common.models` — Claude model default (issue #146).
* :mod:`maki_common.endpoints` — health-endpoint dict built from the URLs
  below plus the port table (issue #137). Uses the same ``<NAME>_URL``
  override convention so a caller that reads ``RECALL_URL`` from here and
  another that reads it via the health dict never disagree.
"""

from __future__ import annotations

import os

# --- NATS ------------------------------------------------------------------

NATS_URL = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN")

# --- Service URLs ----------------------------------------------------------
# ``<NAME>_URL`` override convention — matches maki_common.endpoints so the
# two never drift.

RECALL_URL = os.environ.get("RECALL_URL", "http://maki-recall:8000")
SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "http://maki-synapse:8080")
STEM_URL = os.environ.get("STEM_URL", "http://maki-stem:8000")
CORTEX_URL = os.environ.get("CORTEX_URL", "http://maki-cortex:8080")
FINBERT_URL = os.environ.get("FINBERT_URL", "http://maki-finbert:8080")

# --- Repo identity ---------------------------------------------------------

REPO_OWNER = os.environ.get("REPO_OWNER", "adhityaravi")
REPO_NAME = os.environ.get("REPO_NAME", "maki")
REPO_PATH = os.environ.get("REPO_PATH", "/repo/maki")

__all__ = [
    "CORTEX_URL",
    "FINBERT_URL",
    "NATS_TOKEN",
    "NATS_URL",
    "RECALL_URL",
    "REPO_NAME",
    "REPO_OWNER",
    "REPO_PATH",
    "STEM_URL",
    "SYNAPSE_URL",
]
