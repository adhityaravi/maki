"""Context window size guard middleware."""

from __future__ import annotations

from .base import Middleware, MiddlewareContext, MiddlewareRejection

# Conservative character-based limits. 1 token ~ 4 chars on average.
# Claude models support up to 200k tokens (~800k chars), but we set a
# safe default well below that to leave room for system prompt + response.
DEFAULT_MAX_PROMPT_CHARS = 600_000  # ~150k tokens
DEFAULT_MAX_SYSTEM_CHARS = 200_000  # ~50k tokens
DEFAULT_MAX_TOTAL_CHARS = 700_000  # ~175k tokens


class SizeGuard(Middleware):
    """Rejects requests where the prompt exceeds a configurable size threshold.

    Uses character counts as a fast proxy for token counts (no tokenizer needed).
    """

    def __init__(
        self,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
        max_system_chars: int = DEFAULT_MAX_SYSTEM_CHARS,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    ) -> None:
        self.max_prompt_chars = max_prompt_chars
        self.max_system_chars = max_system_chars
        self.max_total_chars = max_total_chars

    async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
        prompt_len = len(ctx.prompt)
        system_len = len(ctx.system_prompt) if ctx.system_prompt else 0
        total_len = prompt_len + system_len

        if prompt_len > self.max_prompt_chars:
            raise MiddlewareRejection(
                f"Prompt too large: {prompt_len:,} chars (limit {self.max_prompt_chars:,})",
                middleware_name=self.name,
            )

        if system_len > self.max_system_chars:
            raise MiddlewareRejection(
                f"System prompt too large: {system_len:,} chars (limit {self.max_system_chars:,})",
                middleware_name=self.name,
            )

        if total_len > self.max_total_chars:
            raise MiddlewareRejection(
                f"Total context too large: {total_len:,} chars (limit {self.max_total_chars:,})",
                middleware_name=self.name,
            )

        ctx.annotations["size_guard"] = {
            "prompt_chars": prompt_len,
            "system_chars": system_len,
            "total_chars": total_len,
        }

        return ctx
