"""Central source of truth for Maki component service URLs.

Historically ``HEALTH_ENDPOINTS`` was redefined per-service (cortex, stem,
immune) with drifting port literals and inconsistent membership. Adding or
renaming a service meant a three-file edit that was routinely half-done,
and the key naming diverged (``"recall"`` vs ``"maki-recall"``) — which
caused subtle lookup misses when stem merged immune's state into its own
system-state snapshot. See issue #137.

This module owns the port table and the ``<NAME>_URL`` environment-variable
override convention. Every caller reaches for :func:`default_health_endpoints`
so the defaults stay in lockstep.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

# Well-known TCP port each Maki service listens on inside the cluster.
# This is the ONE place these numbers live — add new services here.
DEFAULT_PORTS: dict[str, int] = {
    "stem": 8000,
    "cortex": 8080,
    "recall": 8000,
    "synapse": 8080,
}


def default_health_endpoints(
    prefix: str = "",
    *,
    include: Iterable[str] | None = None,
) -> dict[str, str]:
    """Build ``{name: url}`` for Maki services.

    Each URL falls back to ``http://maki-<name>:<port>`` and is overridable
    via the ``<NAME>_URL`` environment variable (e.g. ``STEM_URL``,
    ``CORTEX_URL``) — matching the convention the three drifting copies
    used before this consolidation.

    Args:
        prefix: String prepended to every key. Use ``"maki-"`` for callers
            that key on Kubernetes ``app=`` labels (immune's HTTP verdicts
            merge with k8s pod verdicts by app label, so the two must
            agree); leave empty for callers that use bare names (cortex,
            stem, tool-facing dicts).
        include: Optional whitelist of service names (bare form) to emit.
            Order is preserved. When omitted, all services in
            :data:`DEFAULT_PORTS` are returned.

    Returns:
        A fresh dict — callers may mutate it (e.g. cortex overrides its
        own ``"cortex"`` entry with a ``localhost`` URL so the local
        HTTP probe hits its own process directly rather than the
        Service, which round-robins to peers).
    """
    names = list(include) if include is not None else list(DEFAULT_PORTS)
    return {
        f"{prefix}{name}": os.environ.get(
            f"{name.upper()}_URL",
            f"http://maki-{name}:{DEFAULT_PORTS[name]}",
        )
        for name in names
    }
