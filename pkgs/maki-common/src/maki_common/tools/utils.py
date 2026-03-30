"""Shared utilities for MCP tool handlers."""

from __future__ import annotations

from typing import Any


def mcp_result(text: str) -> dict[str, Any]:
    """Format a plain-text string as an MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}
