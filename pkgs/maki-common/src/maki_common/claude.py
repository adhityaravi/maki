"""Claude Agent SDK invocation wrapper."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token usage and cost captured from a Claude SDK ResultMessage."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float | None = None
    num_turns: int = 0
    model: str = ""
    mode: str = ""
    duration_ms: float = 0.0
    # per-model breakdown from ResultMessage.model_usage
    model_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "num_turns": self.num_turns,
            "model": self.model,
            "mode": self.mode,
            "duration_ms": round(self.duration_ms, 1),
        }


def _parse_usage(result_message: Any, model: str, mode: str, duration_ms: float) -> TokenUsage:
    """Extract TokenUsage from a Claude SDK ResultMessage."""
    usage_dict = result_message.usage or {}
    model_usage = {}
    raw_model_usage = getattr(result_message, "model_usage", None)
    if raw_model_usage:
        for m, mu in raw_model_usage.items():
            if hasattr(mu, "__dict__"):
                model_usage[m] = mu.__dict__
            elif isinstance(mu, dict):
                model_usage[m] = mu
    return TokenUsage(
        input_tokens=usage_dict.get("input_tokens", 0),
        output_tokens=usage_dict.get("output_tokens", 0),
        cache_read_tokens=usage_dict.get("cache_read_input_tokens", 0),
        cache_creation_tokens=usage_dict.get("cache_creation_input_tokens", 0),
        total_cost_usd=getattr(result_message, "total_cost_usd", None),
        num_turns=getattr(result_message, "num_turns", 0) or 0,
        model=model,
        mode=mode,
        duration_ms=duration_ms,
        model_usage=model_usage,
    )


async def invoke_claude(
    prompt: str,
    model: str = "claude-sonnet-4-20250514",
    semaphore: asyncio.Semaphore | None = None,
    max_turns: int = 1,
    mcp_servers: dict[str, Any] | None = None,
    mode: str = "",
) -> tuple[str, TokenUsage]:
    """Claude invocation via Agent SDK.

    Args:
        prompt: The full prompt to send.
        model: Claude model ID.
        semaphore: Optional concurrency limiter.
        max_turns: Max agentic turns (default 1 for single-shot).
        mcp_servers: Optional MCP servers for tool use.
        mode: Turn mode label for usage tracking (e.g. "idle_reflection", "work").

    Returns:
        Tuple of (response_text, token_usage).
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        mcp_servers=mcp_servers or {},
    )

    async def _invoke() -> tuple[str, TokenUsage]:
        text_parts: list[str] = []
        usage = TokenUsage(model=model, mode=mode)
        t0 = time.monotonic()
        log.info(
            "Invoking Claude",
            extra={
                "model": model,
                "prompt_len": len(prompt),
                "max_turns": max_turns,
                "mcp_server_count": len(mcp_servers) if mcp_servers else 0,
                "mode": mode,
            },
        )
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                duration_ms = (time.monotonic() - t0) * 1000
                usage = _parse_usage(message, model=model, mode=mode, duration_ms=duration_ms)
                log.info("Token usage", extra=usage.to_log_dict())

        result = "\n".join(text_parts)
        log.info("Claude response received", extra={"response_len": len(result)})
        return result, usage

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
    mode: str = "",
    usage_out: list[TokenUsage] | None = None,
) -> AsyncIterator[str]:
    """Stream Claude responses, yielding each assistant text block as it arrives.

    Supports multi-turn with MCP tools. Each yield is a complete text block
    from one assistant message — may span multiple turns if tools are used.

    Args:
        usage_out: If provided, a TokenUsage object is appended to this list
                   when the stream completes (from ResultMessage).
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        mcp_servers=mcp_servers or {},
    )

    async def _stream() -> AsyncIterator[str]:
        t0 = time.monotonic()
        extra = {"model": model, "max_turns": max_turns, "prompt_len": len(prompt), "mode": mode}
        log.info("Streaming Claude", extra=extra)
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        log.info("Stream chunk", extra={"chunk_len": len(block.text)})
                        yield block.text
            elif isinstance(message, ResultMessage):
                duration_ms = (time.monotonic() - t0) * 1000
                usage = _parse_usage(message, model=model, mode=mode, duration_ms=duration_ms)
                log.info("Token usage", extra=usage.to_log_dict())
                if usage_out is not None:
                    usage_out.append(usage)
        log.info("Stream complete")

    if semaphore:
        async with semaphore:
            async for chunk in _stream():
                yield chunk
    else:
        async for chunk in _stream():
            yield chunk
