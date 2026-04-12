"""Tests for the middleware pipeline."""

from __future__ import annotations

import asyncio

import pytest  # type: ignore[import-untyped]
from maki_common.middleware import (
    Middleware,
    MiddlewareContext,
    MiddlewarePipeline,
    MiddlewareRejection,
)
from maki_common.middleware.audit import AuditLogger
from maki_common.middleware.pii import PIIScrubber
from maki_common.middleware.pipeline import get_default_pipeline
from maki_common.middleware.secrets import SecretDetector
from maki_common.middleware.size_guard import SizeGuard


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --- Pipeline basics ---


class TestPipeline:
    def test_empty_pipeline_passes_through(self):
        pipeline = MiddlewarePipeline()
        ctx = MiddlewareContext(prompt="hello", model="test")
        result = _run(pipeline.run(ctx))
        assert result.prompt == "hello"

    def test_middleware_chain_order(self):
        class Appender(Middleware):
            def __init__(self, tag: str):
                self._tag = tag

            async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
                ctx.prompt += f" [{self._tag}]"
                return ctx

        pipeline = MiddlewarePipeline()
        pipeline.add(Appender("A")).add(Appender("B"))
        ctx = MiddlewareContext(prompt="start")
        result = _run(pipeline.run(ctx))
        assert result.prompt == "start [A] [B]"

    def test_short_circuit(self):
        class Rejecter(Middleware):
            async def process(self, ctx: MiddlewareContext) -> MiddlewareContext:
                raise MiddlewareRejection("nope", middleware_name=self.name)

        pipeline = MiddlewarePipeline()
        pipeline.add(Rejecter())
        ctx = MiddlewareContext(prompt="hello")
        with pytest.raises(MiddlewareRejection, match="nope"):
            _run(pipeline.run(ctx))

    def test_default_pipeline_has_all_v1(self):
        # Reset global state
        import maki_common.middleware.pipeline as mod

        mod._default_pipeline = None
        pipeline = get_default_pipeline()
        names = [mw.name for mw in pipeline.middlewares]
        assert "PIIScrubber" in names
        assert "SecretDetector" in names
        assert "SizeGuard" in names
        assert "AuditLogger" in names
        mod._default_pipeline = None  # cleanup


# --- PII scrubber ---


class TestPIIScrubber:
    def test_scrubs_email(self):
        scrubber = PIIScrubber()
        ctx = MiddlewareContext(prompt="Contact me at adi@example.com please")
        result = _run(scrubber.process(ctx))
        assert "[REDACTED:PII]" in result.prompt
        assert "adi@example.com" not in result.prompt
        assert result.annotations["redactions"][0]["counts"]["email"] == 1

    def test_scrubs_phone(self):
        scrubber = PIIScrubber()
        ctx = MiddlewareContext(prompt="Call me at (555) 123-4567")
        result = _run(scrubber.process(ctx))
        assert "[REDACTED:PII]" in result.prompt
        assert "555" not in result.prompt

    def test_scrubs_ssn(self):
        scrubber = PIIScrubber()
        ctx = MiddlewareContext(prompt="SSN: 123-45-6789")
        result = _run(scrubber.process(ctx))
        assert "[REDACTED:PII]" in result.prompt
        assert "123-45-6789" not in result.prompt

    def test_no_false_positive_on_clean_text(self):
        scrubber = PIIScrubber()
        ctx = MiddlewareContext(prompt="Just a normal sentence about coding.")
        result = _run(scrubber.process(ctx))
        assert result.prompt == "Just a normal sentence about coding."
        assert "redactions" not in result.annotations

    def test_scrubs_system_prompt_too(self):
        scrubber = PIIScrubber()
        ctx = MiddlewareContext(prompt="hi", system_prompt="Owner: adi@example.com")
        result = _run(scrubber.process(ctx))
        assert "adi@example.com" not in (result.system_prompt or "")


# --- Secret detector ---


class TestSecretDetector:
    def test_scrubs_github_token(self):
        detector = SecretDetector()
        token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
        ctx = MiddlewareContext(prompt=f"Token: {token}")
        result = _run(detector.process(ctx))
        assert "[REDACTED:SECRET]" in result.prompt
        assert token not in result.prompt

    def test_scrubs_aws_key(self):
        detector = SecretDetector()
        ctx = MiddlewareContext(prompt="AWS key: AKIAIOSFODNN7EXAMPLE")
        result = _run(detector.process(ctx))
        assert "[REDACTED:SECRET]" in result.prompt
        assert "AKIAIOSFODNN7EXAMPLE" not in result.prompt

    def test_scrubs_generic_api_key(self):
        detector = SecretDetector()
        ctx = MiddlewareContext(prompt="api_key=sk_live_abc123def456ghi789jkl012mno")
        result = _run(detector.process(ctx))
        assert "[REDACTED:SECRET]" in result.prompt

    def test_scrubs_pem_key(self):
        detector = SecretDetector()
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
        ctx = MiddlewareContext(prompt=f"Key:\n{pem}")
        result = _run(detector.process(ctx))
        assert "BEGIN PRIVATE KEY" not in result.prompt

    def test_no_false_positive_on_clean_text(self):
        detector = SecretDetector()
        ctx = MiddlewareContext(prompt="Deploy the service to production.")
        result = _run(detector.process(ctx))
        assert result.prompt == "Deploy the service to production."


# --- Size guard ---


class TestSizeGuard:
    def test_allows_small_prompt(self):
        guard = SizeGuard()
        ctx = MiddlewareContext(prompt="hello")
        result = _run(guard.process(ctx))
        assert result.prompt == "hello"
        assert "size_guard" in result.annotations

    def test_rejects_oversized_prompt(self):
        guard = SizeGuard(max_prompt_chars=100)
        ctx = MiddlewareContext(prompt="x" * 200)
        with pytest.raises(MiddlewareRejection, match="Prompt too large"):
            _run(guard.process(ctx))

    def test_rejects_oversized_system_prompt(self):
        guard = SizeGuard(max_system_chars=50)
        ctx = MiddlewareContext(prompt="hi", system_prompt="x" * 100)
        with pytest.raises(MiddlewareRejection, match="System prompt too large"):
            _run(guard.process(ctx))

    def test_rejects_oversized_total(self):
        guard = SizeGuard(max_prompt_chars=500, max_system_chars=500, max_total_chars=100)
        ctx = MiddlewareContext(prompt="x" * 60, system_prompt="y" * 60)
        with pytest.raises(MiddlewareRejection, match="Total context too large"):
            _run(guard.process(ctx))


# --- Audit logger ---


class TestAuditLogger:
    def test_passes_through_without_error(self):
        audit = AuditLogger()
        ctx = MiddlewareContext(prompt="hello")
        result = _run(audit.process(ctx))
        assert result.prompt == "hello"

    def test_logs_redaction_annotations(self):
        audit = AuditLogger()
        ctx = MiddlewareContext(prompt="hello")
        ctx.annotations["redactions"] = [{"middleware": "PIIScrubber", "counts": {"email": 2}}]
        result = _run(audit.process(ctx))
        assert result.prompt == "hello"  # audit doesn't mutate
