"""Claude Agent SDK invocation wrapper."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


async def invoke_claude(
    prompt: str,
    model: str = "claude-sonnet-4-20250514",
    semaphore: asyncio.Semaphore | None = None,
    max_turns: int = 1,
) -> str:
    """Single-shot Claude invocation via Agent SDK.

    Args:
        prompt: The full prompt to send.
        model: Claude model ID.
        semaphore: Optional concurrency limiter.
        max_turns: Max agentic turns (default 1 for single-shot).
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )

    async def _invoke() -> str:
        text_parts: list[str] = []
        log.info("Invoking Claude", extra={"model": model, "prompt_len": len(prompt)})
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
        result = "\n".join(text_parts)
        log.info("Claude response received", extra={"response_len": len(result)})
        return result

    if semaphore:
        async with semaphore:
            return await _invoke()
    return await _invoke()
