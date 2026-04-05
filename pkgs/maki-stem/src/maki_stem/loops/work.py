"""Night work loop — autonomous issue processing during work hours."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

from maki_common import kv_get_float, strip_tags
from maki_common.subjects import CORTEX_STUCK, CORTEX_TURN_REQUEST, DEPLOY_REQUEST

from .base import USER_INACTIVE_THRESHOLD, LoopSpec, StemContext

log = logging.getLogger(__name__)

WORK_CHECK_INTERVAL = int(os.environ.get("WORK_CHECK_INTERVAL", "300"))
WORK_TURN_TIMEOUT = int(os.environ.get("WORK_TURN_TIMEOUT", "2700"))  # 45 minutes

WORK_SKIP_LABELS = {"draft", "human"}
ALLOWED_ISSUE_AUTHORS: frozenset[str] = frozenset({"adhityaravi", "makiself[bot]", "renovate[bot]", "dependabot[bot]"})

KV_KEY = "identity"
_DEFAULT_IDENTITY_FALLBACK = "You are Maki."

# Module-level nightly counters
_work_items_tonight: int = 0
_work_items_tonight_date: str = ""


def _issue_has_skip_label(issue: dict) -> bool:
    """Return True if the issue carries any label that the work loop must skip."""
    for lbl in issue.get("labels", []):
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        if name.lower() in WORK_SKIP_LABELS:
            return True
    return False


def _is_verified_issue_author(issue: dict) -> bool:
    """Return True if the issue was filed by a trusted author."""
    login = issue.get("user", {}).get("login", "")
    return login in ALLOWED_ISSUE_AUTHORS


async def _reject_unverified_issues(issues: list[dict], ctx: StemContext) -> list[dict]:
    """Comment on and close issues from unverified authors. Returns only verified issues."""
    if not ctx.github:
        return issues
    verified = []
    for issue in issues:
        login = issue.get("user", {}).get("login", "")
        if _is_verified_issue_author(issue):
            verified.append(issue)
        else:
            number = issue.get("number")
            log.warning(
                "Rejecting issue from unverified author",
                extra={"number": number, "author": login},
            )
            try:
                await ctx.github.comment_issue(
                    number,
                    f"Closing: this issue was filed by `{login}` who is not a verified contributor. "
                    "Only issues from trusted accounts are processed by the autonomous work loop.",
                )
                await ctx.github.close_issue(number)
            except Exception:
                log.exception("Failed to reject issue", extra={"number": number})
    return verified


async def _request_deploy_after_work(issue_number: int, issue_title: str, ctx: StemContext) -> None:
    """Request a deploy to maki-immune after a successful work session and comment with the result."""
    deploy_target = "maki-immune"
    log.info("Requesting deploy after work", extra={"service": deploy_target, "issue": issue_number})
    try:
        reply = await ctx.nc.request(
            DEPLOY_REQUEST,
            json.dumps({"service": deploy_target, "image_tag": "latest"}).encode(),
            timeout=30.0,
        )
        result = json.loads(reply.data.decode())
        status = result.get("status", "unknown")
        message = result.get("message", "")
        log.info("Deploy requested", extra={"service": deploy_target, "status": status})

        msg_part = f"\n{message}" if message else ""
        deploy_comment = (
            f"🚀 **Deploy requested** → `{deploy_target}`\n\n"
            f"Status: `{status}`{msg_part}\n\n"
            f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        )
    except Exception:
        log.exception("Failed to request deploy after work", extra={"issue": issue_number})
        deploy_comment = (
            f"⚠️ **Deploy request failed** for `{deploy_target}` — will need manual trigger.\n\n"
            f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    if ctx.github:
        await ctx.github.comment_issue(issue_number, deploy_comment)


async def _work_pre_claim_guard(config: dict, ctx: StemContext) -> bool:
    """Pre-claim guard for the work loop: only proceed during work hours."""
    return ctx.in_work_hours(config)


async def _work_should_run(config: dict, ctx: StemContext) -> bool:
    """Post-claim guard checks for the work loop: nightly cap, user inactivity, GitHub."""
    global _work_items_tonight, _work_items_tonight_date

    # Reset nightly counter if date changed
    tonight = datetime.now().strftime("%Y-%m-%d")
    if tonight != _work_items_tonight_date:
        _work_items_tonight = 0
        _work_items_tonight_date = tonight

    max_items = config.get("max_work_items_per_night", 2)
    if _work_items_tonight >= max_items:
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
    global _work_items_tonight

    max_items = config.get("max_work_items_per_night", 2)

    issues = await ctx.github.list_issues(state="open")
    if not issues:
        return

    # Skip issues gated for human review or still in draft
    issues = [i for i in issues if not _issue_has_skip_label(i)]
    if not issues:
        log.info("All open issues are draft or human-gated — skipping work cycle")
        return

    # Reject and close issues from unverified authors (security boundary)
    issues = await _reject_unverified_issues(issues, ctx)
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

    asyncio.create_task(
        ctx.github.comment_issue(
            issue_number,
            f"🔧 **Starting work on this task.**\n\nTime: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        )
    )

    # Load identity
    try:
        entry = await ctx.kv.get(KV_KEY)
        identity = entry.value.decode()
    except Exception:
        identity = _DEFAULT_IDENTITY_FALLBACK

    memories, graph_context = await ctx.search_memories(f"{issue_title} {issue_body[:200]}")

    # Fetch issue comments so cortex has full history before starting (#39)
    issue_comments = await ctx.github.get_issue_comments(issue_number)

    turn_id = f"work-{uuid.uuid4().hex[:8]}"
    work_payload = {
        "turn_id": turn_id,
        "mode": "work",
        "identity": identity,
        "conversation": [],
        "memories": memories,
        "graph_context": graph_context,
        "prompt": None,
        "work_context": {
            "issue_number": issue_number,
            "issue_title": issue_title,
            "issue_description": issue_body,
            "issue_priority": issue_priority,
            "issue_comments": issue_comments,
        },
    }

    queue = ctx.pending.create(turn_id)
    try:
        await ctx.nc.publish(CORTEX_TURN_REQUEST, json.dumps(work_payload).encode())
        log.info("Work turn published", extra={"turn_id": turn_id, "issue": issue_number})

        response_data = await asyncio.wait_for(queue.get(), timeout=WORK_TURN_TIMEOUT)
        result_text = response_data.get("response", "")
        _work_items_tonight += 1

        clean_result = strip_tags(result_text or "")
        log.info(
            "Work turn complete",
            extra={
                "turn_id": turn_id,
                "issue": issue_number,
                "work_items_tonight": _work_items_tonight,
                "max": max_items,
            },
        )

        asyncio.create_task(
            ctx.feed_memories(
                f"[Night work] Task: {issue_title} (priority P{issue_priority})",
                clean_result or "Task completed",
            )
        )

        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        close_comment = f"✅ **Task completed.**\n\n{clean_result}\n\nTime: {ts}"
        asyncio.create_task(ctx.github.close_issue(issue_number, comment=close_comment))

        # Request deploy to maki-immune after successful work (#40)
        asyncio.create_task(_request_deploy_after_work(issue_number, issue_title, ctx))

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

        asyncio.create_task(
            ctx.github.comment_issue(
                issue_number,
                f"⏱️ **Work timed out** after {WORK_TURN_TIMEOUT}s. Will retry next work session.",
            )
        )

    except Exception:
        log.exception("Work turn failed", extra={"turn_id": turn_id, "issue": issue_number})

        # Comment on issue about failure (issue stays open for retry)
        asyncio.create_task(
            ctx.github.comment_issue(
                issue_number,
                "❌ **Work failed** due to an error. Will retry next work session.",
            )
        )

    finally:
        ctx.pending.remove(turn_id)

    # Cooldown between work items
    cooldown = config.get("work_cooldown_minutes", 15) * 60
    log.info("Work cooldown", extra={"cooldown_seconds": cooldown})
    await asyncio.sleep(cooldown)


WORK_LOOP_SPEC = LoopSpec(
    name="work",
    check_interval_getter=lambda: WORK_CHECK_INTERVAL,
    execution_interval_getter=lambda config: config.get("work_interval", WORK_CHECK_INTERVAL),
    pre_claim_guard=_work_pre_claim_guard,
    should_run=_work_should_run,
    body=_work_body,
)
