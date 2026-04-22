"""Night work loop — autonomous issue processing during work hours."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

from maki_common import kv_get_float, spawn_background, strip_tags
from maki_common.subjects import CORTEX_STUCK, CORTEX_TURN_REQUEST

from .base import (
    UNKNOWN_ISSUER_LABEL,
    USER_INACTIVE_THRESHOLD,
    LoopSpec,
    StemContext,
    assemble_loop_prompt,
    cron_window,
    issue_has_label,
    load_identity,
    tag_unverified_issues,
)

log = logging.getLogger(__name__)

WORK_CHECK_INTERVAL = int(os.environ.get("WORK_CHECK_INTERVAL", "300"))
WORK_TURN_TIMEOUT = int(os.environ.get("WORK_TURN_TIMEOUT", "2700"))  # 45 minutes

WORK_SKIP_LABELS = {"draft", "human", UNKNOWN_ISSUER_LABEL}

# Work fires at 03:00 every day
WORK_CRON = "0 3 * * *"

# Prompt caching note
# -------------------
# The Claude Code CLI (which the Agent SDK uses) wraps the system prompt with
# Anthropic prompt-caching breakpoints. Caching is a *prefix* match, so every
# byte before the first variable character must stay byte-identical across
# turns for the cache to hit. We therefore split the work prompt into a static
# header (mode description, instructions, rules — identical across every work
# turn) and a dynamic task block (issue #, title, description, comments) that
# changes per turn. The final system prompt is assembled by
# :func:`assemble_loop_prompt` as:
#
#     identity            (static, shared by every loop)
#   + TOOLS_PROMPT        (static, shared by every loop)
#   + _WORK_STATIC_PROMPT (static across all work turns)
#   + _WORK_TASK_TEMPLATE (dynamic — filled from the issue)
#   + memories / graph    (dynamic)
#
# Everything up to and including _WORK_STATIC_PROMPT is a stable ~2 KTok
# prefix that the API can serve from cache at 90% discount on subsequent
# tool-use turns inside a single streaming conversation.
_WORK_STATIC_PROMPT = """## Work Mode

You have a GitHub issue to execute. Complete it fully — code changes, commit, \
push, build, deploy if needed. You have every tool available.

## Instructions
1. Understand the task. Use search_code and read_file to study relevant code.
2. Implement changes with write_file.
3. Rebuild the code graph with rebuild_code_graph after changes.
4. **Run quality_check before committing.** Fix any lint or format issues it finds.
5. Commit and push with git_commit_and_push.
6. CI builds Docker images automatically on push. Use get_workflow_status to verify builds succeed. \
Wait for CI to complete before deploying — images won't exist until then.
7. Deploy only the affected components using request_deploy with the SHA tag from the push \
(format: sha-<first 7 chars of commit>). Deployable components: stem, cortex, immune, ears, \
recall, synapse. If maki-common was changed, deploy all of them. \
Immune monitors health and auto-rollbacks if unhealthy.
8. When done, close the issue with close_issue and a brief result summary.
9. Store any learnings with add_memory.

## Rules
- Execute the task. Don't just plan — do it.
- If the task is unclear, do your best interpretation.
- If blocked or too risky, comment on the issue with why and leave it open.
- If a task is truly impossible to solve autonomously (requires physical access, credentials \
you cannot obtain, or human judgment that cannot be automated), use add_label to add the \
"human" label, comment on the issue explaining why, then leave it open. Do NOT close it — \
Adi will handle it.
- **Open a PR instead of pushing directly when any of the following apply:** \
(1) changes involve Terraform/OpenTofu, SOPS/secrets, or new Kubernetes manifests; \
(2) the issue description or comments explicitly ask for a PR. \
When opening a PR: assign it to adhityaravi, use add_label to add "human" to the issue, \
comment the PR link on the issue, and leave the issue open. Do NOT close it — Adi will \
review and merge.
- Be brief in your response. Report what you did, not what you plan to do.
- One task at a time. Focus.

## Context
Relevant memories for this task have been preloaded — check the "Relevant memories" and \
"Relationships" sections below before starting. Use them to inform your approach. \
Read the issue comments — they may contain clarifications, design decisions, or \
explicit instructions that override the original description."""


_WORK_TASK_TEMPLATE = """## Task
Issue: #{issue_number}
Title: {issue_title}
Description: {issue_description}
Priority: {issue_priority}
Comments: {issue_comments}"""


def _build_work_system_prompt(
    identity: str,
    memories: list,
    graph_context: list,
    work_context: dict,
) -> str:
    """Assemble the complete system prompt for a work turn.

    Delegates the shared layout (identity + tools + memories + graph) to
    :func:`assemble_loop_prompt` and only formats the work-specific main
    section here.
    """
    raw_comments = work_context.get("issue_comments", [])
    if raw_comments:
        comment_lines = [
            f"  [{c.get('created_at', '')}] @{c.get('author', 'unknown')}: {c.get('body', '')}"  # noqa: E501
            for c in raw_comments
        ]
        issue_comments_str = "\n" + "\n".join(comment_lines)
    else:
        issue_comments_str = "None"

    dynamic = _WORK_TASK_TEMPLATE.format(
        issue_number=work_context.get("issue_number", "?"),
        issue_title=work_context.get("issue_title", "?"),
        issue_description=work_context.get("issue_description", "No description provided."),
        issue_priority=work_context.get("issue_priority", "?"),
        issue_comments=issue_comments_str,
    )
    main_section = f"{_WORK_STATIC_PROMPT}\n\n{dynamic}"

    return assemble_loop_prompt(identity, main_section, memories, graph_context)


def _issue_has_skip_label(issue: dict) -> bool:
    """Return True if the issue carries any label that the work loop must skip."""
    return any(issue_has_label(issue, lbl) for lbl in WORK_SKIP_LABELS)


async def _work_pre_claim_guard(config: dict, ctx: StemContext) -> bool:
    """Pre-claim guard for the work loop: only proceed when the cron window is open."""
    return cron_window(WORK_CRON)


async def _work_should_run(config: dict, ctx: StemContext) -> bool:
    """Post-claim guard: user inactivity and GitHub availability."""
    # Only work if user has been inactive
    last_activity = await kv_get_float(ctx.lock_kv, "stem.last_activity", default=time.time())
    if time.time() - last_activity < USER_INACTIVE_THRESHOLD:
        log.info("Work loop: user recently active, skipping")
        return False

    if not ctx.github:
        return False

    return True


async def _work_body(spec: LoopSpec, config: dict, ctx: StemContext) -> None:
    """Execute one night work cycle: pick an issue and hand it to cortex."""
    issues = await ctx.github.list_issues(state="open")
    if not issues:
        return

    # Skip issues gated for human review or still in draft
    issues = [i for i in issues if not _issue_has_skip_label(i)]
    if not issues:
        log.info("All open issues are draft or human-gated — skipping work cycle")
        return

    # Tag and filter issues from unverified authors (security boundary)
    issues = await tag_unverified_issues(issues, ctx)
    if not issues:
        log.info("No verified-author issues remain after author check — skipping work cycle")
        return

    issue = issues[0]  # Highest priority (list_issues sorts by P-label)
    issue_number = issue["number"]
    issue_title = issue["title"]
    issue_body = issue.get("body", "") or ""

    # Extract priority from labels (default P3)
    issue_priority = 3
    for label in issue.get("labels", []):
        label_name = label.get("name", "") if isinstance(label, dict) else str(label)
        if label_name in ("P1", "P2", "P3", "P4", "P5"):
            issue_priority = int(label_name[1])
            break

    log.info(
        "Work loop: starting task",
        extra={"issue": issue_number, "title": issue_title, "priority": issue_priority},
    )

    spawn_background(
        ctx.github.comment_issue(
            issue_number,
            f"🔧 **Starting work on this task.**\n\nTime: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        ),
        name="work.start_comment",
    )

    identity = await load_identity(ctx.kv)

    memories, graph_context = await ctx.search_memories(f"{issue_title} {issue_body[:200]}")

    # Fetch issue comments so cortex has full history before starting (#39)
    issue_comments = await ctx.github.get_issue_comments(issue_number)

    turn_id = f"work-{uuid.uuid4().hex[:8]}"
    work_context = {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_description": issue_body,
        "issue_priority": issue_priority,
        "issue_comments": issue_comments,
    }
    work_payload = {
        "turn_id": turn_id,
        "mode": "work",
        "identity": identity,
        "conversation": [],
        "memories": memories,
        "graph_context": graph_context,
        "prompt": "Execute this task.",
        "stream": False,
        "max_turns": int(os.environ.get("CORTEX_WORK_MAX_TURNS", "100")),
        "git_pull": True,
        "work_context": work_context,
        "system_prompt": _build_work_system_prompt(identity, memories, graph_context, work_context),
        **({"model": spec.model} if spec.model else {}),
    }

    queue = ctx.pending.create(turn_id)
    try:
        await ctx.nc.publish(CORTEX_TURN_REQUEST, json.dumps(work_payload).encode())
        log.info("Work turn published", extra={"turn_id": turn_id, "issue": issue_number})

        response_data = await asyncio.wait_for(queue.get(), timeout=WORK_TURN_TIMEOUT)
        result_text = response_data.get("response", "")
        clean_result = strip_tags(result_text or "")
        log.info(
            "Work turn complete",
            extra={"turn_id": turn_id, "issue": issue_number},
        )

        spawn_background(
            ctx.feed_memories(
                f"[Night work] Task: {issue_title} (priority P{issue_priority})",
                clean_result or "Task completed",
            ),
            name="work.feed_memories",
        )

        # Re-fetch issue to check if cortex added the "human" label (e.g. opened a PR
        # for infra changes and left it open for Adi to review). If so, skip auto-close.
        refreshed = await ctx.github.get_issue(issue_number)
        if refreshed and _issue_has_skip_label(refreshed):
            log.info(
                "Issue has human/draft label after work — skipping auto-close",
                extra={"issue": issue_number},
            )
        else:
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            close_comment = f"✅ **Task completed.**\n\n{clean_result}\n\nTime: {ts}"
            spawn_background(
                ctx.github.close_issue(issue_number, comment=close_comment),
                name="work.close_issue",
            )

    except TimeoutError:
        log.error("Work turn timed out", extra={"turn_id": turn_id, "issue": issue_number})
        # Publish stuck signal for immune
        await ctx.nc.publish(
            CORTEX_STUCK,
            json.dumps(
                {
                    "turn_id": turn_id,
                    "mode": "work",
                    "timeout_seconds": WORK_TURN_TIMEOUT,
                    "user_waiting": False,
                }
            ).encode(),
        )

        spawn_background(
            ctx.github.comment_issue(
                issue_number,
                f"⏱️ **Work timed out** after {WORK_TURN_TIMEOUT}s. Will retry next work session.",
            ),
            name="work.timeout_comment",
        )

    except Exception:
        log.exception("Work turn failed", extra={"turn_id": turn_id, "issue": issue_number})

        # Comment on issue about failure (issue stays open for retry)
        spawn_background(
            ctx.github.comment_issue(
                issue_number,
                "❌ **Work failed** due to an error. Will retry next work session.",
            ),
            name="work.failure_comment",
        )

    finally:
        ctx.pending.remove(turn_id)


WORK_LOOP_SPEC = LoopSpec(
    name="work",
    check_interval_getter=lambda: WORK_CHECK_INTERVAL,
    execution_interval_getter=lambda config: 86400,  # once per day — lock TTL prevents double-fire
    pre_claim_guard=_work_pre_claim_guard,
    should_run=_work_should_run,
    body=_work_body,
    model="claude-opus-4-7",
)
