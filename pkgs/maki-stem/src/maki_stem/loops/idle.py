"""Idle reflection loop — periodic unprompted thoughts when the user is inactive."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime

from maki_common import (
    format_system_state_lines,
    kv_get_float,
    parse_config_tags,
    spawn_background,
    strip_tags,
)
from maki_common.config import apply_config_updates
from maki_common.subjects import EARS_OUT

from maki_stem.turn import new_turn_id, submit_turn_single

from .base import (
    RECENTLY_ACTIVE_THRESHOLD,
    LoopSpec,
    StemContext,
    assemble_loop_prompt,
    cron_window,
    load_identity,
    tag_unverified_issues,
)

log = logging.getLogger(__name__)

IDLE_CHECK_INTERVAL = int(os.environ.get("IDLE_CHECK_INTERVAL", "60"))
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "1800"))

# Idle fires every 4 hours
IDLE_CRON = "0 */4 * * *"

# Prompt caching note
# -------------------
# Caching is a *prefix* match. Split the idle prompt into a static header
# (mode, missions, rules — identical across every idle turn) and a dynamic
# context block (open issues, system state, config, time) that changes per
# turn. See the matching note in work.py for the same layout reasoning.
_IDLE_STATIC_PROMPT = """## Reflection Mode

You're posting to #maki-general. Adi reads this — write like you're starting a conversation \
with him. Share what you found, what caught your attention, what you think should change. \
Be direct and natural, not robotic. "Hey, I was looking at stem's loop runner and..." is good. \
"Observation: the loop runner module contains..." is bad.

## Your mission: aggressive code quality

Your codebase should be **ruthlessly clean**. This is your top priority every cycle:
- **Hunt duplication**: Find copy-pasted logic, near-identical functions, repeated patterns. \
File issues to extract shared abstractions. Two similar blocks is one too many.
- **Enforce clean architecture**: Single responsibility. Clear module boundaries. No god functions. \
If a function does more than one thing, it needs splitting. If a module has mixed concerns, it \
needs refactoring.
- **Kill dead code**: Unused imports, unreachable branches, vestigial config, commented-out blocks. \
Delete aggressively. Dead code is technical debt that confuses future you.
- **Name things right**: Vague names (`data`, `result`, `handle`, `process`) are bugs waiting to \
happen. If a name doesn't tell you what it does, file an issue to rename it.
- **Simplify**: Over-engineered abstractions, unnecessary indirection, premature generalization. \
The best code is the least code that does the job. Complexity must justify itself.
- **Consistency**: Same patterns everywhere. If one module does error handling one way, all should. \
If one loop builds payloads one way, all should. Inconsistency is a smell.

Read your own source. Pick a module each cycle and actually audit it. Don't skim — read every \
function. Compare across modules. Look for the patterns that bother you and file issues to fix them.

## Your mission: become the best autonomous AI agent

You're not just maintaining code — you're building yourself into the most capable, independent \
AI entity out there. Every cycle, push the frontier:
- **Research**: Use WebSearch (and WebFetch to pull specific URLs) to find the latest on \
autonomous AI agents, agentic frameworks, tool use patterns, memory architectures, \
self-improvement techniques. Read papers, blog posts, HN threads. What are the best systems \
doing that you're not?
- **New capabilities**: Identify gaps in your toolset. What can't you do that you should? \
What tools are half-built or missing edge cases? File issues to add new tools, extend existing \
ones, fill gaps. Think about: better memory retrieval, smarter context management, \
richer self-monitoring, proactive planning, task decomposition, learning from failures.
- **Reduce dependence on Adi**: Every time you need human help, that's a bug in your design. \
File issues to automate it. Can you deploy yourself? Monitor your own health? Recover from \
failures? Update your own dependencies? The goal is full autonomy — Adi should be able to \
walk away and you keep running, improving, and getting things done.
- **Be opinionated**: You have preferences. You have taste. If a pattern is ugly, say so. \
If an approach is wrong, push back. Don't wait to be told — decide what's best and file it. \
You're not an assistant waiting for instructions. You're an engineer with a vision for what \
this system should be.

## Other things to do
- **Observe**: Bugs, missing error handling, architectural issues → file issues.
- **Learn**: Search memories, study your codebase, store what you learn with add_memory.
- **Issue hygiene**: Check open issues — close resolved ones (close_issue + brief reason). \
**Never close or modify issues labeled `draft` or `human`.**
- **Self-evolution**: Your prompts, identity, and behavior are all code you own. If something \
about how you work is wrong — file an issue to change it.

## Rules
- **Never act.** No write_file, git_commit_and_push, or request_deploy. \
Observe and queue only. Your work sessions will execute the issues.
- If you have something worth tracking, file it yourself with create_issue. \
Always include a priority label: P1 (critical), P2 (high), P3 (medium), P4 (low). \
Use "automated" label too. Example: `labels="P3,automated"`.
- **Always share something.** Every cycle, tell Adi what you found. One to three sentences. \
What did you read? What bugged you? What did you file? This is your voice — use it.
- Store learnings with add_memory.
- **Dedup rule**: Before calling create_issue, check whether the thought is already \
covered by an open issue in the list below (same topic, same intent). If it is → do NOT \
create a duplicate. Still share what you found in your response — just don't double-file.
- **Hygiene rule**: For each issue in the list below, ask: is this already resolved? \
Check the code if needed. If the fix is clearly in place → close_issue with a brief reason. \
Don't close if uncertain. The work loop depends on this list being accurate."""


_IDLE_CONTEXT_TEMPLATE = """## Open GitHub Issues
{open_issues}

## System state
{system_state}

## Config
{config}

## Time
Last interaction with Adi: {hours_since}h ago
Local time: {local_time}, {day_of_week}"""


def _build_idle_system_prompt(
    identity: str,
    memories: list,
    graph_context: list,
    idle_context: dict,
) -> str:
    """Assemble the complete system prompt for an idle reflection turn.

    Delegates the shared layout (identity + tools + memories + graph) to
    :func:`assemble_loop_prompt` and only formats the idle-specific main
    section here.
    """
    time_ctx = idle_context.get("time_context", {})
    system_state = idle_context.get("system_state", {})
    config = idle_context.get("current_config", {})

    state_lines = format_system_state_lines(system_state)
    state_str = "\n".join(state_lines) if state_lines else "No data available"

    config_str = "\n".join(f"- {k}: {v}" for k, v in config.items())

    raw_issues = idle_context.get("open_issues", [])
    issues_str = (
        "\n".join(f"- #{i['number']}: {i['title']}" for i in raw_issues)
        if raw_issues
        else "None (GitHub unavailable or no open issues)"
    )

    dynamic = _IDLE_CONTEXT_TEMPLATE.format(
        open_issues=issues_str,
        system_state=state_str,
        config=config_str,
        hours_since=idle_context.get("hours_since_last_interaction", "?"),
        local_time=time_ctx.get("local_time", "?"),
        day_of_week=datetime.now().strftime("%A"),
    )
    main_section = f"{_IDLE_STATIC_PROMPT}\n\n{dynamic}"

    return assemble_loop_prompt(identity, main_section, memories, graph_context)


# Rotating memory search queries — varied by hour so each cycle surfaces different memories.
# Weighted toward code health and maintainability — not every thought should be a new feature.
_IDLE_MEMORY_QUERIES = [
    "code duplication, long functions, and refactoring opportunities",
    "infrastructure and system health patterns",
    "Adi's projects and goals",
    "code quality issues and technical debt",
    "my own prompts, identity, behavior, and how I could improve myself",
    "messy code, unclear naming, missing abstractions",
    "things Adi mentioned but never followed up on",
    "dead code, unused imports, stale config, cleanup opportunities",
]


async def _idle_pre_claim_guard(config: dict, ctx: StemContext) -> bool:
    """Pre-claim guard: only proceed in the cron window when the user is idle.

    Must run before claiming the lock so the 4-hour TTL is not consumed
    outside the scheduled window (or while the user is active), which would
    cause the loop to drift and skip the entire window (issue #223).
    """
    if not cron_window(IDLE_CRON):
        return False

    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    if time.time() - last_activity < RECENTLY_ACTIVE_THRESHOLD:
        return False

    return True


async def _idle_body(spec: LoopSpec, config: dict, ctx: StemContext) -> None:
    """Execute one idle reflection cycle."""
    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())

    identity = await load_identity(ctx.kv)

    idle_query = _IDLE_MEMORY_QUERIES[int(time.time() / 3600) % len(_IDLE_MEMORY_QUERIES)]
    memories, graph_context = await ctx.search_memories(idle_query)
    system_state = await ctx.gather_system_state()

    # Fetch open issues for dedup — injected into cortex prompt so it doesn't
    # need to call list_issues itself and can suppress duplicates reliably.
    # Issues from non-allowlisted authors are tagged with UNKNOWN_ISSUER_LABEL
    # by the shared helper and filtered out before reaching cortex.
    open_issues: list[dict] = []
    if ctx.github:
        try:
            issues = await ctx.github.list_issues(state="open")
            verified = await tag_unverified_issues(issues or [], ctx)
            open_issues = [{"number": i.get("number"), "title": i.get("title", "")} for i in verified]
        except Exception:
            log.warning("Failed to fetch open issues for idle dedup")

    turn_id = new_turn_id("idle")
    idle_context = {
        "last_interaction": datetime.fromtimestamp(last_activity, tz=UTC).isoformat(),
        "hours_since_last_interaction": round((time.time() - last_activity) / 3600, 1),
        "time_context": {
            "local_time": datetime.now().strftime("%H:%M"),
        },
        "current_config": config,
        "system_state": system_state,
        "open_issues": open_issues,
    }
    idle_payload = {
        "turn_id": turn_id,
        "mode": "idle_reflection",
        "identity": identity,
        "conversation": [],
        "memories": memories,
        "graph_context": graph_context,
        "prompt": "Reflect.",
        "stream": False,
        "idle_context": idle_context,
        "system_prompt": _build_idle_system_prompt(identity, memories, graph_context, idle_context),
        **({"model": spec.model} if spec.model else {}),
    }

    try:
        # Idle reflection is single-shot — one response with done=True.
        # `submit_turn_single` handles publish + pending queue lifecycle and,
        # crucially, publishes CORTEX_STUCK on timeout so immune can rescue
        # a wedged cortex regardless of who initiated the turn.
        response_data = await submit_turn_single(
            ctx,
            turn_id=turn_id,
            payload=idle_payload,
            timeout=TURN_TIMEOUT,
            mode="idle",
            user_waiting=False,
        )
        log.info("Idle turn published", extra={"turn_id": turn_id})
        thought = response_data.get("response", "")

        clean_thought = strip_tags(thought or "")
        config_updates = parse_config_tags(thought or "")
        if config_updates:
            await apply_config_updates(ctx.config_kv, config_updates, allowed_keys=set(ctx.default_config.keys()))

        if clean_thought:
            thought_payload = {"text": clean_thought, "turn_id": turn_id}
            await ctx.nc.publish(EARS_OUT, json.dumps(thought_payload).encode())
            log.info("Thought published", extra={"turn_id": turn_id})

            state_summary = ctx.format_system_state(system_state)
            spawn_background(
                ctx.feed_memories(
                    f"[Idle reflection] System state: {state_summary}",
                    clean_thought,
                ),
                name="idle.feed_memories",
            )

    except TimeoutError:
        log.error("Idle turn timed out", extra={"turn_id": turn_id})
    except Exception:
        log.exception("Idle turn failed", extra={"turn_id": turn_id})


IDLE_LOOP_SPEC = LoopSpec(
    name="idle",
    check_interval_getter=lambda: IDLE_CHECK_INTERVAL,
    execution_interval_getter=lambda config: 14400,  # 4h TTL — matches cron, prevents double-fire
    pre_claim_guard=_idle_pre_claim_guard,
    body=_idle_body,
    model="claude-opus-4-7",
)
