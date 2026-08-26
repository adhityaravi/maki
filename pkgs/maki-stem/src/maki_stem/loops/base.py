"""Base types and generic loop runner shared by all background loops."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter
from maki_common import (
    format_graph_block,
    format_memories_block,
    kv_put_float,
    load_kv_config,
    try_claim_loop,
)

log = logging.getLogger(__name__)

# Shared thresholds used by multiple loops
RECENTLY_ACTIVE_THRESHOLD = 600  # 10 minutes
USER_INACTIVE_THRESHOLD = 7200  # 2 hours

# Issue author allowlist — only issues from these accounts are processed by any loop.
# Unknown-author issues get tagged with UNKNOWN_ISSUER_LABEL and ignored.
UNKNOWN_ISSUER_LABEL = "unknown-issuer"
ALLOWED_ISSUE_AUTHORS: frozenset[str] = frozenset({"adhityaravi", "makiself[bot]", "renovate[bot]", "dependabot[bot]"})

# Identity KV — shared by every loop that needs to prepend Maki's self-description.
IDENTITY_KV_KEY = "identity"
DEFAULT_IDENTITY = "You are Maki."


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


async def tag_unverified_issues(issues: list[dict], ctx: StemContext) -> list[dict]:
    """Tag issues from unverified authors with UNKNOWN_ISSUER_LABEL.

    Returns the subset of issues whose author is in the allowlist — i.e. the
    issues a loop is allowed to act on. Issues from unverified authors that
    are not yet tagged get labeled inline (awaited) so the next pass sees the
    label and won't re-tag.

    Awaiting (rather than fire-and-forget) is the safer default: it keeps the
    label-set authoritative across overlapping cycles. Per-call latency is a
    handful of GitHub API requests at most — well within the loops' budgets.
    Callers that don't need the label persisted before their *current* pass
    completes can ignore this property; the security boundary is preserved
    either way because unverified issues are always filtered out.
    """
    if not ctx.github:
        return issues
    verified: list[dict] = []
    for issue in issues:
        if is_verified_issue_author(issue):
            verified.append(issue)
            continue
        if issue_has_label(issue, UNKNOWN_ISSUER_LABEL):
            continue
        number = issue.get("number")
        login = issue.get("user", {}).get("login", "")
        log.warning(
            "Tagging issue from unverified author",
            extra={"number": number, "author": login},
        )
        try:
            await ctx.github.add_label(number, UNKNOWN_ISSUER_LABEL)
        except Exception:
            log.exception("Failed to tag unverified issue", extra={"number": number})
    return verified


# How long the cron window stays open — loop must fire within this many seconds of the scheduled time
CRON_WINDOW_SECONDS = 1800  # 30 minutes

# Shared tools listing injected into every loop system prompt.
# Lives here so all loops stay in sync — edit once, affects all.
TOOLS_PROMPT = """## Tools
Memory: search_memories, get_all_memories, add_memory, get_system_health, check_component, \
get_pod_logs (live kubectl logs via immune — use previous=true for CrashLoopBackOff stack traces), \
get_config, update_config
Code: search_code (use FIRST — scopes: symbol/callers/callees/references/definition/file/path), \
read_file, write_file, list_directory, search_text, rebuild_code_graph
Git: git_status, git_diff, quality_check (run before commit), git_commit_and_push, git_pull, \
get_workflow_status, get_workflow_logs
Deploy: request_deploy, get_deploy_status
Issues: create_issue, list_issues, get_issue, close_issue, comment_issue, add_label, remove_label
Web: WebSearch (query the web for recent info), WebFetch (fetch a URL and extract content)

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


async def load_identity(kv: Any) -> str:
    """Load Maki's identity string from the shared KV, falling back to a safe default.

    Every loop prepends identity to its system prompt. Centralising the load here
    keeps the try/except block and fallback string in one place so future loops
    don't re-implement the same boilerplate.
    """
    try:
        entry = await kv.get(IDENTITY_KV_KEY)
        return entry.value.decode()
    except Exception:
        return DEFAULT_IDENTITY


def assemble_loop_prompt(
    identity: str,
    static_header: str,
    dynamic_context: str,
    memories: list,
    graph_context: list,
) -> tuple[str, str]:
    """Split the loop prompt into a byte-stable system prompt and a per-turn human prefix.

    Anthropic prompt caching is a *prefix* match keyed off the last
    ``cache_control`` breakpoint. The Claude Agent SDK marks the end of
    ``system_prompt`` as such a breakpoint, so anything byte-stable across
    turns MUST live in the returned ``system_prompt`` and anything that
    changes per turn MUST live in the returned ``human_prefix`` (which the
    caller concatenates onto the human message). Concatenating dynamic
    content into the system prompt — the previous shape of this helper —
    silently killed cache reuse on every turn (issue #437).

    Split:

        system_prompt (stable → cached):
            identity        (loaded once from KV per turn; changes only when Adi edits)
          + TOOLS_PROMPT    (module constant, shared by every loop)
          + static_header   (loop-specific mode/rules text)

        human_prefix (rebuilt per turn):
            dynamic_context (per-turn task/context: issue details, system state, etc.)
          + memories_block  (freshly retrieved per turn)
          + graph_block     (freshly retrieved per turn)

    The caller merges ``human_prefix`` into its human-turn ``prompt`` (typically
    ``f"{human_prefix}\\n\\n{prompt}"``). Returning both strings — rather than
    burying the split in each loop — keeps every loop's cache story identical.
    """
    # --- Static prefix (cacheable) ---
    system_parts: list[str] = []
    if identity:
        system_parts.append(identity)
    system_parts.append(TOOLS_PROMPT)
    if static_header:
        system_parts.append(static_header)
    system_prompt = "\n\n".join(system_parts)

    # --- Dynamic tail (changes per turn) — goes into the human message ---
    human_parts: list[str] = []
    if dynamic_context:
        human_parts.append(dynamic_context)

    memories_block = format_memories_block(memories)
    if memories_block:
        human_parts.append(memories_block)

    graph_block = format_graph_block(graph_context)
    if graph_block:
        human_parts.append(graph_block)

    human_prefix = "\n\n".join(human_parts)
    return system_prompt, human_prefix


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

    Each loop shares the same outer structure: sleep → load config → run guard →
    claim lock → execute body. LoopSpec captures the per-loop variation so a single
    generic runner (_run_loop) can drive all loops without duplicating that skeleton.
    """

    name: str
    """Human-readable name used in log messages and as the NATS KV lock key suffix."""

    check_interval_getter: Callable[[], int]
    """Returns how often (seconds) the loop wakes up to *check* whether it should run."""

    execution_interval_getter: Callable[[dict], int]
    """Returns the minimum interval (seconds) between actual executions (claim TTL)."""

    body: Callable[[LoopSpec, dict, StemContext], Coroutine[None, None, None]]  # type: ignore[type-arg]
    """Async callable(spec, config, ctx) that performs the actual loop work for one cycle."""

    pre_claim_guard: Callable[[dict, StemContext], Coroutine[None, None, bool]] | None = None  # type: ignore[type-arg]
    """Optional async callable(config, ctx) → bool evaluated *before* claiming the lock.

    Return False to skip this cycle. Guards MUST run before the lock claim: a claim
    consumes the execution_interval TTL even when no body runs, so a post-claim veto
    would burn an entire interval on a skipped cycle (issue #223).
    """

    model: str | None = None
    """Claude model override for this loop's cortex turns. None = cortex default."""

    extra: dict = field(default_factory=dict)
    """Arbitrary per-loop state accessible inside *body* (e.g. daily counters)."""


async def _run_loop(spec: LoopSpec, ctx: StemContext) -> None:
    """Generic loop runner — drives any LoopSpec with the shared scheduling skeleton.

    Handles: periodic sleep, config loading, optional pre-claim guard, distributed
    lock claiming, and top-level exception isolation. Per-loop variation lives
    entirely inside *spec.pre_claim_guard* and *spec.body*.
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
            await spec.body(spec, config, ctx)
            try:
                await kv_put_float(ctx.lock_kv, f"loop.heartbeat.{spec.name}", time.time())
            except Exception:
                log.warning("Failed to write loop heartbeat", extra={"loop": spec.name})
        except Exception:
            log.exception("Error in loop", extra={"loop": spec.name})
