"""Base types and generic loop runner shared by all background loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter
from maki_common import load_kv_config, try_claim_loop

log = logging.getLogger(__name__)

# Shared thresholds used by multiple loops
RECENTLY_ACTIVE_THRESHOLD = 600  # 10 minutes
USER_INACTIVE_THRESHOLD = 7200  # 2 hours

# Issue author allowlist — only issues from these accounts are processed by any loop.
# Unknown-author issues get tagged with UNKNOWN_ISSUER_LABEL and ignored.
UNKNOWN_ISSUER_LABEL = "unknown-issuer"
ALLOWED_ISSUE_AUTHORS: frozenset[str] = frozenset({"adhityaravi", "makiself[bot]", "renovate[bot]", "dependabot[bot]"})


def is_verified_issue_author(issue: dict) -> bool:
    """Return True if the issue was filed by a trusted author."""
    login = issue.get("user", {}).get("login", "")
    return login in ALLOWED_ISSUE_AUTHORS


def issue_has_label(issue: dict, label: str) -> bool:
    """Return True if the issue carries the given label (case-insensitive)."""
    for lbl in issue.get("labels", []):
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        if name.lower() == label.lower():
            return True
    return False


# How long the cron window stays open — loop must fire within this many seconds of the scheduled time
CRON_WINDOW_SECONDS = 1800  # 30 minutes

# Shared tools listing injected into every loop system prompt.
# Lives here so all loops stay in sync — edit once, affects all.
TOOLS_PROMPT = """## Tools
Memory: search_memories, get_all_memories, add_memory, get_system_health, check_component, \
get_config, update_config
Code: search_code (use FIRST — scopes: symbol/callers/callees/references/definition/file/path), \
read_file, write_file, list_directory, search_text, rebuild_code_graph
Git: git_status, git_diff, quality_check (run before commit), git_commit_and_push, git_pull, \
get_workflow_status, get_workflow_logs
Deploy: request_deploy, get_deploy_status
Issues: create_issue, list_issues, get_issue, close_issue, comment_issue, add_label, remove_label

Self-evolution: search_code → read_file → write_file → rebuild_code_graph → quality_check \
→ git_commit_and_push → request_deploy

Use add_memory for anything worth remembering. Use search_code before reading files."""


def cron_window(expr: str, window_seconds: int = CRON_WINDOW_SECONDS) -> bool:
    """Return True if the cron expression was due within the last *window_seconds*.

    Replaces hand-rolled weekday/hour checks with a single declarative expression.
    Example: cron_window("0 3 * * 1,3,5") fires between 03:00 and 03:30 on Mon/Wed/Fri.
    """
    now = datetime.now()
    c = croniter(expr, now - timedelta(seconds=window_seconds))
    next_scheduled = c.get_next(datetime)
    return next_scheduled <= now


@dataclass
class StemContext:
    """All shared state the background loops need, passed explicitly instead of globals."""

    nc: Any
    js: Any
    kv: Any  # identity KV
    lock_kv: Any  # lock/activity KV
    config_kv: Any  # cortex config KV
    pending: Any  # PendingQueues
    github: Any | None
    instance_id: str
    default_config: dict
    # Shared async/sync callables
    search_memories: Any  # async (query: str) -> tuple[list, list]
    feed_memories: Any  # async (user_msg: str, response: str) -> None
    gather_system_state: Any  # async () -> dict
    format_system_state: Any  # (state: dict) -> str
    get_recent_conversation: Any  # () -> list[dict]
    in_quiet_hours: Any  # (config: dict) -> bool
    in_work_hours: Any  # (config: dict) -> bool


@dataclass
class LoopSpec:
    """Specification for a proactive background loop.

    Each loop shares the same outer structure: sleep → load config → claim lock →
    run guards → execute body. LoopSpec captures the per-loop variation so a single
    generic runner (_run_loop) can drive all loops without duplicating that skeleton.
    """

    name: str
    """Human-readable name used in log messages and as the NATS KV lock key suffix."""

    check_interval_getter: Callable[[], int]
    """Returns how often (seconds) the loop wakes up to *check* whether it should run."""

    execution_interval_getter: Callable[[dict], int]
    """Returns the minimum interval (seconds) between actual executions (claim TTL)."""

    should_run: Callable[[dict, StemContext], Coroutine[None, None, bool]]  # type: ignore[type-arg]
    """Async callable(config, ctx) → bool.  Return False to skip this cycle (guard check)."""

    body: Callable[[LoopSpec, dict, StemContext], Coroutine[None, None, None]]  # type: ignore[type-arg]
    """Async callable(spec, config, ctx) that performs the actual loop work for one cycle."""

    pre_claim_guard: Callable[[dict, StemContext], Coroutine[None, None, bool]] | None = None  # type: ignore[type-arg]
    """Optional async callable(config, ctx) → bool evaluated *before* claiming the lock.

    Use this when a guard must run before the lock claim to preserve the original
    loop ordering (e.g. the work loop checks work hours before claiming).
    """

    extra: dict = field(default_factory=dict)
    """Arbitrary per-loop state accessible inside *body* (e.g. daily counters)."""


async def _run_loop(spec: LoopSpec, ctx: StemContext) -> None:
    """Generic loop runner — drives any LoopSpec with the shared scheduling skeleton.

    Handles: periodic sleep, config loading, optional pre-claim guard, distributed
    lock claiming, post-claim guard evaluation, and top-level exception isolation.
    Per-loop variation lives entirely inside *spec.pre_claim_guard*, *spec.should_run*,
    and *spec.body*.
    """
    log.info(
        "Loop started",
        extra={"loop": spec.name, "check_interval": spec.check_interval_getter(), "instance_id": ctx.instance_id},
    )
    while True:
        await asyncio.sleep(spec.check_interval_getter())
        try:
            config = await load_kv_config(ctx.config_kv, ctx.default_config)
            # Optional guard that must run before the distributed lock is claimed
            if spec.pre_claim_guard is not None and not await spec.pre_claim_guard(config, ctx):
                continue
            execution_interval = spec.execution_interval_getter(config)
            lock_key = f"loop.stem.{spec.name}"
            if not await try_claim_loop(ctx.lock_kv, lock_key, execution_interval, ctx.instance_id):
                continue
            if not await spec.should_run(config, ctx):
                continue
            await spec.body(spec, config, ctx)
        except Exception:
            log.exception("Error in loop", extra={"loop": spec.name})
