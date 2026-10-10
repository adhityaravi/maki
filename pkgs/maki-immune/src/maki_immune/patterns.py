"""Error pattern matching and confidence model for passive log monitoring.

Queries known error patterns from vault (via NATS → stem), matches them against
log candidates, and decides whether to suppress (known no-op) or escalate.

Confidence model:
- First write: confidence=0.5, occurrence_count=1
- Each match against an already-trusted no-op pattern: confidence += 0.1,
  occurrence_count += 1 (reinforces what we already know).
- Untrusted matches escalate to Claude and DO NOT bump stats — otherwise the
  pattern self-promotes to trusted before Claude has confirmed anything
  (issue #342). A future "Claude confirmed no-op" signal can bump stats on
  the escalation path.
- Trusted threshold: confidence >= 0.9 OR occurrence_count >= 3
  → pattern is silently suppressed without Claude involvement
"""

import json
import logging
import re
from typing import Any

from maki_common.subjects import PATTERN_QUERY, PATTERN_UPDATE, PATTERN_WRITE

log = logging.getLogger(__name__)

_QUERY_TIMEOUT = 5.0  # seconds
_CONFIDENCE_INCREMENT = 0.1
_CONFIDENCE_THRESHOLD = 0.9
_OCCURRENCE_THRESHOLD = 3


async def query_patterns(nc: Any, component: str) -> list[dict]:
    """Fetch known error patterns for a component from vault via NATS."""
    try:
        resp = await nc.request(
            PATTERN_QUERY,
            json.dumps({"component": component}).encode(),
            timeout=_QUERY_TIMEOUT,
        )
        data = json.loads(resp.data.decode())
        return data.get("patterns", [])
    except Exception:
        log.warning("Failed to query patterns", extra={"component": component})
        return []


async def update_pattern_stats(nc: Any, pattern_id: str, confidence_delta: float = 0.0) -> None:
    """Fire-and-forget update: bump occurrence_count + last_seen_at for a pattern."""
    try:
        await nc.publish(
            PATTERN_UPDATE,
            json.dumps({"id": pattern_id, "confidence_delta": confidence_delta}).encode(),
        )
    except Exception:
        log.debug("Failed to publish pattern update", extra={"pattern_id": pattern_id})


def _is_trusted(pattern: dict) -> bool:
    """Check if a pattern has reached the trusted threshold."""
    return (
        pattern.get("confidence", 0) >= _CONFIDENCE_THRESHOLD
        or pattern.get("occurrence_count", 0) >= _OCCURRENCE_THRESHOLD
    )


def match_candidate(candidate: dict, patterns: list[dict]) -> dict | None:
    """Try to match a candidate's log tail against known patterns.

    Returns the first matching pattern dict, or None if no match.
    """
    log_text = candidate.get("log_tail", "")
    if not log_text:
        return None

    for pat in patterns:
        regex = pat.get("pattern", "")
        if not regex:
            continue
        try:
            if re.search(regex, log_text):
                return pat
        except re.error:
            log.warning("Invalid regex in error_patterns", extra={"pattern_id": pat.get("id"), "regex": regex})
            continue

    return None


async def write_pattern(nc: Any, pattern: dict) -> None:
    """Fire-and-forget write: insert a newly classified pattern into error_patterns via stem."""
    try:
        await nc.publish(
            PATTERN_WRITE,
            json.dumps(pattern).encode(),
        )
    except Exception:
        log.warning("Failed to publish pattern write", extra={"component": pattern.get("component")})


async def check_candidates(
    nc: Any,
    candidates: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Check a list of error candidates against known patterns.

    Returns (suppressed, escalate) where:
    - suppressed: candidates matched by trusted no-op patterns (silently handled)
    - escalate: candidates that need Claude attention (unknown or untrusted)
    """
    suppressed: list[dict] = []
    escalate: list[dict] = []

    # Group candidates by component for efficient querying
    by_component: dict[str, list[dict]] = {}
    for c in candidates:
        by_component.setdefault(c["component"], []).append(c)

    for component, component_candidates in by_component.items():
        patterns = await query_patterns(nc, component)

        for candidate in component_candidates:
            matched_pattern = match_candidate(candidate, patterns)

            if matched_pattern is None:
                # Unknown pattern — escalate
                candidate["reason"] = "unknown_pattern"
                escalate.append(candidate)
                continue

            is_noop = matched_pattern.get("classification") == "no_op"

            if is_noop and _is_trusted(matched_pattern):
                # Trusted no-op — suppress silently, bump stats
                await update_pattern_stats(nc, matched_pattern["id"], _CONFIDENCE_INCREMENT)
                candidate["matched_pattern_id"] = matched_pattern["id"]
                candidate["matched_pattern_notes"] = matched_pattern.get("notes", "")
                suppressed.append(candidate)
                log.debug(
                    "Suppressed known no-op pattern",
                    extra={
                        "component": component,
                        "pattern_id": matched_pattern["id"],
                        "confidence": matched_pattern["confidence"],
                        "occurrence_count": matched_pattern["occurrence_count"],
                    },
                )
            else:
                # Either not no-op, or not yet trusted — escalate.
                # Do NOT bump stats here: occurrence_count drives trust promotion
                # (_is_trusted), and bumping on every suspected match would let a
                # brand-new no_op pattern self-promote to trusted in 2 hits without
                # Claude ever confirming it. Stats should only grow on *confirmed*
                # occurrences (the trusted-suppression path above, or a future
                # explicit "Claude confirmed no-op" signal). See issue #342.
                candidate["matched_pattern_id"] = matched_pattern["id"]
                candidate["reason"] = (
                    "untrusted_noop" if is_noop else f"classified_{matched_pattern.get('classification')}"
                )
                escalate.append(candidate)

    return suppressed, escalate
