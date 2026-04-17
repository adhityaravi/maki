"""PII scrubber middleware — regex-based, zero LLM calls."""

from __future__ import annotations

import re

from .base import Middleware, MiddlewareContext

_REDACTED = "[REDACTED:PII]"

# --- Patterns ---

# Email addresses
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Phone numbers (US/international formats)
_PHONE_RE = re.compile(
    r"(?<!\d)"  # not preceded by digit
    r"(?:\+?1[\s\-.]?)?"  # optional country code
    r"(?:\(?\d{3}\)?[\s\-.]?)"  # area code
    r"\d{3}[\s\-.]?"  # exchange
    r"\d{4}"  # subscriber
    r"(?!\d)",  # not followed by digit
)

# SSN (US)
_SSN_RE = re.compile(r"\b\d{3}[\-\s]\d{2}[\-\s]\d{4}\b")

# Credit card numbers (basic — 13-19 digits, with optional separators)
_CC_RE = re.compile(r"\b(?:\d[\s\-]?){13,19}\b")

_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_SSN_RE, "ssn"),
    (_CC_RE, "credit_card"),
    (_EMAIL_RE, "email"),
    (_PHONE_RE, "phone"),
]


def _scrub(text: str, counts: dict[str, int]) -> str:
    """Apply all PII patterns to text, returning scrubbed version."""
    for pattern, label in _PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            counts[label] = counts.get(label, 0) + len(matches)
            text = pattern.sub(_REDACTED, text)
    return text


class PIIScrubber(Middleware):
    """Scrubs PII (emails, phone numbers, SSNs, credit cards) from prompts."""

    async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
        counts: dict[str, int] = {}

        ctx.prompt = _scrub(ctx.prompt, counts)
        if ctx.system_prompt:
            ctx.system_prompt = _scrub(ctx.system_prompt, counts)

        if counts:
            ctx.annotations.setdefault("redactions", []).append({"middleware": self.name, "counts": counts})

        return ctx
