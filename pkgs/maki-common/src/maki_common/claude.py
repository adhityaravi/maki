"""Claude Agent SDK invocation wrapper."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)


async def invoke_claude(
    prompt: str,
    model: str = "claude-sonnet-4-20250514",
    semaphore: asyncio.Semaphore | None = None,
    max_turns: int = 1,
    mcp_servers: dict[str, Any] | None = None,
) -> str:
    """Claude invocation via Agent SDK.

    Args:
        prompt: The full prompt to send.
        model: Claude model ID.
        semaphore: Optional concurrency limiter.
        max_turns: Max agentic turns (default 1 for single-shot).
        mcp_servers: Optional MCP servers for tool use.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        mcp_servers=mcp_servers or {},
    )

    async def _invoke() -> str:
        text_parts: list[str] = []
        log.info(
            "Invoking Claude",
            extra={
                "model": model,
                "prompt_len": len(prompt),
                "max_turns": max_turns,
                "mcp_server_count": len(mcp_servers) if mcp_servers else 0,
            },
        )
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


async def stream_claude(
    prompt: str,
    model: str = "claude-sonnet-4-20250514",
    semaphore: asyncio.Semaphore | None = None,
    max_turns: int = 10,
    mcp_servers: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Stream Claude responses, yielding each assistant text block as it arrives.

    Supports multi-turn with MCP tools. Each yield is a complete text block
    from one assistant message — may span multiple turns if tools are used.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        mcp_servers=mcp_servers or {},
    )

    async def _stream() -> AsyncIterator[str]:
        log.info("Streaming Claude", extra={"model": model, "max_turns": max_turns, "prompt_len": len(prompt)})
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        log.info("Stream chunk", extra={"chunk_len": len(block.text)})
                        yield block.text
        log.info("Stream complete")

    if semaphore:
        async with semaphore:
            async for chunk in _stream():
                yield chunk
    else:
        async for chunk in _stream():
            yield chunk
