"""Claude Agent SDK invocation wrapper."""

from __future__ import annotations

import asyncio
import contextlib
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


def _build_options(
    model: str,
    max_turns: int,
    mcp_servers: dict[str, Any] | None,
    system_prompt: str | None,
) -> Any:
    """Build ClaudeAgentOptions with maki defaults."""
    from claude_agent_sdk import ClaudeAgentOptions

    options_kwargs: dict[str, Any] = dict(
        model=model,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        mcp_servers=mcp_servers or {},
    )
    if system_prompt:
        options_kwargs["system_prompt"] = system_prompt
    return ClaudeAgentOptions(**options_kwargs)


@contextlib.asynccontextmanager
async def _maybe_acquire(semaphore: asyncio.Semaphore | None) -> AsyncIterator[None]:
    """Acquire `semaphore` if provided, otherwise no-op."""
    if semaphore is None:
        yield
    else:
        async with semaphore:
            yield


async def _run_query(
    prompt: str,
    options: Any,
    *,
    model: str,
    mode: str,
    usage_out: list[TokenUsage],
) -> AsyncIterator[str]:
    """Drive the SDK query loop.

    Yields each assistant TextBlock as it arrives. On ResultMessage, parses
    usage, logs it, and appends to ``usage_out``. Swallows the post-result
    CLI exit (benign — SDK process exits cleanly after delivering the result).
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    t0 = time.monotonic()
    got_result = False
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield block.text
            elif isinstance(message, ResultMessage):
                duration_ms = (time.monotonic() - t0) * 1000
                usage = _parse_usage(message, model=model, mode=mode, duration_ms=duration_ms)
                log.info("Token usage", extra=usage.to_log_dict())
                usage_out.append(usage)
                got_result = True
    except Exception:
        if got_result:
            # SDK CLI process exited after delivering the result — benign
            log.debug("SDK process exited after result (expected)")
        else:
            raise


async def invoke_claude(
    prompt: str,
    model: str = "claude-sonnet-4-20250514",
    semaphore: asyncio.Semaphore | None = None,
    max_turns: int = 1,
    mcp_servers: dict[str, Any] | None = None,
    mode: str = "",
    system_prompt: str | None = None,
) -> tuple[str, TokenUsage]:
    """Claude invocation via Agent SDK.

    Args:
        prompt: The human turn prompt (current message + XML-tagged conversation history).
        model: Claude model ID.
        semaphore: Optional concurrency limiter.
        max_turns: Max agentic turns (default 1 for single-shot).
        mcp_servers: Optional MCP servers for tool use.
        mode: Turn mode label for usage tracking (e.g. "idle_reflection", "work").
        system_prompt: Static system context (identity, memories, graph). Kept
            separate from the human prompt so conversation history cannot bleed
            into the system context and vice versa.

    Returns:
        Tuple of (response_text, token_usage).
    """
    options = _build_options(model, max_turns, mcp_servers, system_prompt)
    text_parts: list[str] = []
    usage_box: list[TokenUsage] = []
    async with _maybe_acquire(semaphore):
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
        async for chunk in _run_query(prompt, options, model=model, mode=mode, usage_out=usage_box):
            text_parts.append(chunk)
    result = "\n".join(text_parts)
    log.info("Claude response received", extra={"response_len": len(result)})
    usage = usage_box[0] if usage_box else TokenUsage(model=model, mode=mode)
    return result, usage


async def stream_claude(
    prompt: str,
    model: str = "claude-sonnet-4-20250514",
    semaphore: asyncio.Semaphore | None = None,
    max_turns: int = 10,
    mcp_servers: dict[str, Any] | None = None,
    mode: str = "",
    usage_out: list[TokenUsage] | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """Stream Claude responses, yielding each assistant text block as it arrives.

    Supports multi-turn with MCP tools. Each yield is a complete text block
    from one assistant message — may span multiple turns if tools are used.

    Args:
        usage_out: If provided, a TokenUsage object is appended to this list
                   when the stream completes (from ResultMessage).
        system_prompt: Static system context (identity, memories, graph). Kept
            separate from the human prompt so conversation history cannot bleed
            into the system context and vice versa.
    """
    options = _build_options(model, max_turns, mcp_servers, system_prompt)
    local_usage: list[TokenUsage] = []
    async with _maybe_acquire(semaphore):
        log.info(
            "Streaming Claude",
            extra={"model": model, "max_turns": max_turns, "prompt_len": len(prompt), "mode": mode},
        )
        async for chunk in _run_query(prompt, options, model=model, mode=mode, usage_out=local_usage):
            log.info("Stream chunk", extra={"chunk_len": len(chunk)})
            yield chunk
    if usage_out is not None and local_usage:
        usage_out.append(local_usage[0])
    log.info("Stream complete")
