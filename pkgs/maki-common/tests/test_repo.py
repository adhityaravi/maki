"""Tests for maki_common.repo — primarily the token redaction helper."""

from __future__ import annotations

from maki_common.repo import redact_token


def test_redact_token_strips_url_form() -> None:
    """The exact form git echoes on a 403 must lose the secret."""
    msg = (
        "fatal: unable to access "
        "'https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz1234567890@github.com/owner/repo.git/': "
        "The requested URL returned error: 403"
    )
    redacted = redact_token(msg)
    assert "ghs_" not in redacted
    assert "x-access-token:***@" in redacted
    assert "github.com/owner/repo.git" in redacted


def test_redact_token_strips_bare_token() -> None:
    """Defensive: bare ghs_ / ghp_ tokens get stripped even outside a URL."""
    for prefix in ("ghs_", "ghp_", "gho_", "ghu_", "ghr_"):
        token = f"{prefix}abcdefghijklmnopqrstuvwxyz1234567890"
        msg = f"token leaked: {token} oops"
        redacted = redact_token(msg)
        assert token not in redacted
        assert "***" in redacted


def test_redact_token_handles_multiple_occurrences() -> None:
    msg = (
        "https://x-access-token:ghs_AAAAAAAAAAAAAAAAAAAA@github.com/a/b "
        "and again https://x-access-token:ghs_BBBBBBBBBBBBBBBBBBBB@github.com/c/d"
    )
    redacted = redact_token(msg)
    assert "ghs_AAAA" not in redacted
    assert "ghs_BBBB" not in redacted
    assert redacted.count("x-access-token:***@") == 2


def test_redact_token_passthrough_safe_text() -> None:
    """No tokens → text is returned as-is, no spurious mangling."""
    safe = "Pull failed: refusing to merge unrelated histories"
    assert redact_token(safe) == safe


def test_redact_token_handles_empty_and_none_like() -> None:
    assert redact_token("") == ""
