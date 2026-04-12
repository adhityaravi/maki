"""Secret/token detector middleware — pattern-based, zero LLM calls."""

from __future__ import annotations

import re

from .base import Middleware, MiddlewareContext

_REDACTED = "[REDACTED:SECRET]"

# --- Patterns ---

# Generic API key patterns (key=..., api_key=..., token=...)
_GENERIC_KEY_RE = re.compile(
    r"(?i)"
    r"(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|auth[_\-]?token|bearer)"
    r"[\s]*[=:]\s*"
    r"['\"]?([A-Za-z0-9\-_\.]{20,})['\"]?"
)

# AWS access key IDs (AKIA...)
_AWS_KEY_RE = re.compile(r"\b(AKIA[0-9A-Z]{16})\b")

# AWS secret access keys (40 chars, base64-ish)
_AWS_SECRET_RE = re.compile(r"\b([A-Za-z0-9/+=]{40})\b")

# GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_)
_GITHUB_TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{36,})\b")

# Slack tokens (xoxb-, xoxp-, xoxa-, xoxs-)
_SLACK_TOKEN_RE = re.compile(r"\b(xox[bpas]\-[A-Za-z0-9\-]{24,})\b")

# PEM private keys
_PEM_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END (?:RSA |EC |DSA )?PRIVATE KEY-----"
)

# Generic long hex secrets (64+ hex chars — likely a hash/key)
_HEX_SECRET_RE = re.compile(r"\b([0-9a-fA-F]{64,})\b")

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_PEM_RE, "private_key"),
    (_AWS_KEY_RE, "aws_key"),
    (_GITHUB_TOKEN_RE, "github_token"),
    (_SLACK_TOKEN_RE, "slack_token"),
    (_GENERIC_KEY_RE, "api_key"),
    (_HEX_SECRET_RE, "hex_secret"),
]


def _scrub(text: str, counts: dict[str, int]) -> str:
    """Apply all secret patterns to text, returning scrubbed version."""
    for pattern, label in _SECRET_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            counts[label] = counts.get(label, 0) + len(matches)
            text = pattern.sub(_REDACTED, text)
    return text


class SecretDetector(Middleware):
    """Detects and redacts secrets, API keys, and tokens from prompts."""

    async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
        counts: dict[str, int] = {}

        ctx.prompt = _scrub(ctx.prompt, counts)
        if ctx.system_prompt:
            ctx.system_prompt = _scrub(ctx.system_prompt, counts)

        if counts:
            ctx.annotations.setdefault("redactions", []).append({"middleware": self.name, "counts": counts})

        return ctx
