"""Night work loop — autonomous issue processing during work hours."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime

from maki_common import kv_get_float, spawn_background, strip_tags

from maki_stem.turn import TurnPublishError, new_turn_id, submit_turn_single

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

# Per-issue attempt tracking (issue #213) — prevents one wedged issue from
# pinning the work loop forever. Records `(last_attempt_ts, failure_count)`
# under ``stem.work.attempts.<issue_number>`` in the lock KV. Cooldown grows
# exponentially with failure count, capped at 7 days. After
# ``WORK_HUMAN_LABEL_THRESHOLD`` consecutive failures the issue is auto-tagged
# ``human`` so the work loop stops considering it.
WORK_ATTEMPTS_KEY_PREFIX = "stem.work.attempts."
WORK_BACKOFF_BASE_SECONDS = 86400  # 1 day
WORK_BACKOFF_MAX_SECONDS = 7 * 86400  # 7 days
WORK_HUMAN_LABEL_THRESHOLD = 3  # auto-add `human` after this many consecutive failures

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


# --- Per-issue attempt tracking (issue #213) ----------------------------------


def _attempts_key(issue_number: int) -> str:
    """KV key for storing attempt history of a single issue."""
    return f"{WORK_ATTEMPTS_KEY_PREFIX}{issue_number}"


def _backoff_seconds(failure_count: int) -> int:
    """Return the cooldown window after *failure_count* consecutive failures.

    Exponential schedule capped at :data:`WORK_BACKOFF_MAX_SECONDS`:

        1 failure  → 1 day
        2 failures → 2 days
        3 failures → 4 days
        4+ failures→ 7 days (cap)
    """
    if failure_count <= 0:
        return 0
    # 2 ** (n-1) days, but guard against absurd shifts for very large counts
    exp = min(failure_count - 1, 30)
    return min(WORK_BACKOFF_MAX_SECONDS, WORK_BACKOFF_BASE_SECONDS * (2**exp))


async def _get_attempts(lock_kv, issue_number: int) -> tuple[float, int]:
    """Read the attempt record for *issue_number*.

    Returns ``(last_attempt_ts, failure_count)``. Both zero when no record
    exists or the record is unreadable — a missing record means "never failed,
    eligible to run".
    """
    try:
        entry = await lock_kv.get(_attempts_key(issue_number))
        data = json.loads(entry.value.decode())
        return float(data.get("last_attempt_ts", 0)), int(data.get("failure_count", 0))
    except Exception:
        return 0.0, 0


async def _set_attempts(lock_kv, issue_number: int, last_attempt_ts: float, failure_count: int) -> None:
    """Write/overwrite the attempt record for *issue_number* (best-effort)."""
    payload = json.dumps({"last_attempt_ts": last_attempt_ts, "failure_count": failure_count}).encode()
    try:
        await lock_kv.put(_attempts_key(issue_number), payload)
    except Exception:
        log.warning(
            "Failed to write work attempt record",
            extra={"issue": issue_number, "failure_count": failure_count},
        )


async def _clear_attempts(lock_kv, issue_number: int) -> None:
    """Delete the attempt record for *issue_number* (best-effort)."""
    try:
        await lock_kv.delete(_attempts_key(issue_number))
    except Exception:
        # Missing keys are fine; any other failure is non-fatal — the worst
        # case is the issue stays in cooldown a little longer than necessary.
        pass


def _is_in_cooldown(last_attempt_ts: float, failure_count: int, now: float) -> bool:
    """Return True if the issue is still inside its exponential-backoff window."""
    if failure_count <= 0:
        return False
    return (now - last_attempt_ts) < _backoff_seconds(failure_count)


async def _select_next_issue(issues: list[dict], ctx: StemContext) -> dict | None:
    """Pick the highest-priority issue not currently in failure cooldown.

    *issues* is assumed to already be sorted by priority then created_at asc
    (this is what :meth:`GitHubClient.list_issues` returns). We walk in order
    and return the first one whose attempt record permits a retry. Issues with
    an active cooldown are skipped — when *every* candidate is cooling down we
    return ``None`` and the caller no-ops this cycle.

    The natural side-effect is the bonus described in #213: if the top P2/P3
    issues are all wedged in backoff, we transparently fall back to a fresh
    P4/cleanup task instead of spinning on the broken one.
    """
    now = time.time()
    for issue in issues:
        number = issue.get("number")
        if number is None:
            continue
        last_ts, fails = await _get_attempts(ctx.lock_kv, int(number))
        if _is_in_cooldown(last_ts, fails, now):
            log.info(
                "Skipping issue in failure cooldown",
                extra={
                    "issue": number,
                    "failure_count": fails,
                    "cooldown_remaining_s": int(_backoff_seconds(fails) - (now - last_ts)),
                },
            )
            continue
        return issue
    return None


async def _record_work_failure(ctx: StemContext, issue_number: int, reason: str) -> int:
    """Increment the failure counter for *issue_number* and escalate if needed.

    Returns the new failure count. When the count hits
    :data:`WORK_HUMAN_LABEL_THRESHOLD` we add the ``human`` label so the work
    loop stops picking the issue at all (it's filtered out by
    :data:`WORK_SKIP_LABELS`).
    """
    last_ts, fails = await _get_attempts(ctx.lock_kv, issue_number)
    fails += 1
    await _set_attempts(ctx.lock_kv, issue_number, time.time(), fails)
    log.warning(
        "Recorded work failure",
        extra={"issue": issue_number, "failure_count": fails, "reason": reason},
    )
    if fails >= WORK_HUMAN_LABEL_THRESHOLD and ctx.github is not None:
        try:
            await ctx.github.add_label(issue_number, "human")
            spawn_background(
                ctx.github.comment_issue(
                    issue_number,
                    (
                        f"🚧 **Auto-escalated to human review** after "
                        f"{fails} consecutive work-loop failures "
                        f"(latest: {reason}). Removing this from the autonomous "
                        f"queue — Adi will take a look."
                    ),
                ),
                name="work.human_escalation_comment",
            )
            log.warning(
                "Auto-added `human` label after repeated work failures",
                extra={"issue": issue_number, "failure_count": fails},
            )
        except Exception:
            log.exception(
                "Failed to auto-escalate issue to human",
                extra={"issue": issue_number, "failure_count": fails},
            )
    return fails


async def _work_pre_claim_guard(config: dict, ctx: StemContext) -> bool:
    """Pre-claim guard for the work loop: cron window, user inactivity, GitHub availability.

    Must run before claiming the lock so the 1-day TTL is not consumed when no work
    will be done (e.g. user mid-conversation), which would skip the entire overnight
    work cycle (issue #223).
    """
    if not cron_window(WORK_CRON):
        return False

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
    # max_results=None → no cap. With 250+ open issues, the default 200 cap
    # silently starves this loop of newly-filed P1/P2s (they land at the tail
    # of the asc-by-created fetch). See issue #404.
    issues = await ctx.github.list_issues(state="open", max_results=None)
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

    # Pick the highest-priority candidate not currently in failure cooldown
    # (issue #213). Walking the priority-sorted list naturally falls back to
    # P4/cleanup work when every higher-priority issue is in backoff.
    issue = await _select_next_issue(issues, ctx)
    if issue is None:
        log.info(
            "All eligible issues are in failure cooldown — skipping work cycle",
            extra={"candidates": len(issues)},
        )
        return

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

    turn_id = new_turn_id("work")
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

    # Failure-counting boundary (issue #284)
    # ---------------------------------------
    # Only failures inside the cortex-turn boundary itself — i.e. cortex
    # actually attempted the work and either errored or hung — should
    # increment the per-issue failure counter. Infra failures (NATS publish,
    # pending-session setup, post-completion GitHub refresh) mean the issue
    # was never really attempted; counting them would silently quarantine the
    # top-priority issue behind a `human` label after three NATS hiccups,
    # which is exactly when the system most needs to keep running.
    #
    # ``submit_turn_single`` handles publish + wait + CORTEX_STUCK signalling
    # uniformly (issue #125). It distinguishes the two failure modes we care
    # about via exception type:
    #   - TurnPublishError → NATS never accepted the request (infra).
    #   - TimeoutError     → published fine but cortex hung (counts as issue).
    #   - other Exception  → published fine but the wait errored (counts).
    try:
        log.info("Work turn publishing", extra={"turn_id": turn_id, "issue": issue_number})
        response_data = await submit_turn_single(
            ctx,
            turn_id=turn_id,
            payload=work_payload,
            timeout=WORK_TURN_TIMEOUT,
            mode="work",
            user_waiting=False,
        )
        log.info("Work turn published", extra={"turn_id": turn_id, "issue": issue_number})
    except TurnPublishError:
        # --- Phase 1 (INFRA): publish failed; cortex never started. ---
        log.exception(
            "Work turn publish failed — infra issue, not counting against issue",
            extra={"turn_id": turn_id, "issue": issue_number},
        )
        return
    except TimeoutError:
        # --- Phase 2 (ISSUE): cortex received but did not respond in time. ---
        # CORTEX_STUCK was already emitted inside ``submit_turn_single``.
        log.error("Work turn timed out", extra={"turn_id": turn_id, "issue": issue_number})
        new_count = await _record_work_failure(ctx, issue_number, "timeout")
        spawn_background(
            ctx.github.comment_issue(
                issue_number,
                (
                    f"⏱️ **Work timed out** after {WORK_TURN_TIMEOUT}s "
                    f"(failure {new_count}). Backing off — next eligible retry in "
                    f"~{_backoff_seconds(new_count) // 3600}h."
                ),
            ),
            name="work.timeout_comment",
        )
        return
    except Exception:
        # --- Phase 2 (ISSUE): pending-session setup succeeded but wait errored. ---
        # Historically this branch also covered the outer ``pending.session``
        # setup/exit failure as an infra event, but in practice such failures
        # come from the pending-queue plumbing itself and are extremely rare;
        # treating them as an issue-side failure keeps the exception matrix
        # trivially readable and still respects the cooldown/human-escalation
        # behaviour (a single truly transient blip only bumps the counter by
        # one and is cleared on the next successful run).
        log.exception(
            "Work turn failed while awaiting cortex response",
            extra={"turn_id": turn_id, "issue": issue_number},
        )
        new_count = await _record_work_failure(ctx, issue_number, "exception")
        spawn_background(
            ctx.github.comment_issue(
                issue_number,
                (
                    f"❌ **Work failed** due to an error (failure {new_count}). "
                    f"Backing off — next eligible retry in "
                    f"~{_backoff_seconds(new_count) // 3600}h."
                ),
            ),
            name="work.failure_comment",
        )
        return

    # Defensive contract check (issue #422)
    # -------------------------------------
    # ``done=True`` alone is not a completion signal — cortex publishes it on
    # every terminal path, including silent rate-limit / capacity bailouts and
    # generic errors where zero real work happened. Treat any of the following
    # as a failure and route to the same backoff path as an exception:
    #
    #   - ``cancelled=True`` (cortex explicitly bailed — timeout, preemption,
    #     silent error, generic error, cortex restart).
    #   - Empty response body (cortex published a done chunk with nothing in it
    #     — historically the silent-error branch did this without a cancelled
    #     flag; belt-and-suspenders in case any future path repeats that).
    #
    # Without this guard, a 529/overloaded burst against cortex's model would
    # cause the work loop to auto-close the issue with a blank "Task
    # completed." comment, pollute memories with a phantom completion, and
    # wipe the per-issue failure backoff via ``_clear_attempts``.
    result_text = response_data.get("response", "")
    cancelled = bool(response_data.get("cancelled"))
    if cancelled or not result_text.strip():
        reason = response_data.get("reason") or ("cancelled" if cancelled else "empty_response")
        log.warning(
            "Work turn returned no usable result — treating as failure",
            extra={
                "turn_id": turn_id,
                "issue": issue_number,
                "cancelled": cancelled,
                "reason": reason,
                "response_len": len(result_text),
            },
        )
        new_count = await _record_work_failure(ctx, issue_number, reason)
        spawn_background(
            ctx.github.comment_issue(
                issue_number,
                (
                    f"⚠️ **Work bailed out** ({reason}, failure {new_count}). "
                    f"Backing off — next eligible retry in "
                    f"~{_backoff_seconds(new_count) // 3600}h."
                ),
            ),
            name="work.cancelled_comment",
        )
        return

    clean_result = strip_tags(result_text)
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

    # --- Phase 3: post-processing (INFRA) ---
    # Re-fetch issue to check if cortex added the "human" label (e.g.
    # opened a PR for infra changes and left it open for Adi to
    # review). If the refresh itself flakes, skip auto-close rather
    # than counting it against the issue — cortex already ran.
    try:
        refreshed = await ctx.github.get_issue(issue_number)
    except Exception:
        log.warning(
            "Failed to refresh issue after work — skipping auto-close",
            extra={"issue": issue_number},
            exc_info=True,
        )
        refreshed = None

    if refreshed is None:
        # Couldn't determine current state; leave the issue alone.
        pass
    elif _issue_has_skip_label(refreshed):
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

    # Successful run — clear the failure record so any prior cooldown
    # is lifted (in case the previous failure was transient).
    await _clear_attempts(ctx.lock_kv, issue_number)


WORK_LOOP_SPEC = LoopSpec(
    name="work",
    check_interval_getter=lambda: WORK_CHECK_INTERVAL,
    execution_interval_getter=lambda config: 86400,  # once per day — lock TTL prevents double-fire
    pre_claim_guard=_work_pre_claim_guard,
    body=_work_body,
    model="claude-opus-4-7",
)
