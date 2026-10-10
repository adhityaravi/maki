"""Tests for the CodeGraph MCP tool wrapper.

Regression coverage for issue #521 — the MCP `search_code` tool schema
declared every param as required, and non-empty sentinels like ``"-"``
silently returned zero results because downstream filter logic only treats
``""`` as "no filter".

Uses plain ``asyncio.run`` + ``assert`` so the maki-common test suite stays
pytest-asyncio-free (see ``test_futures.py``, ``test_nats_terminal.py``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from maki_common.tools.codegraph_tools import (
    _NO_FILTER_SENTINELS,
    _norm_filter,
    make_codegraph_tools,
)


class _StubRegistry:
    """Minimal RepoRegistry stand-in — resolves to a fake workspace path."""

    def __init__(self, path: str = "/tmp/does-not-matter") -> None:
        self._path = path

    async def resolve(self, key: str | None):
        return SimpleNamespace(path=self._path)

    def known(self) -> list[str]:
        return ["maki"]


def _get_search_code():
    tools = make_codegraph_tools(_StubRegistry())  # type: ignore[arg-type]
    by_name = {name: (desc, schema, handler) for name, desc, schema, handler in tools}
    return by_name["search_code"]


# --------------------------------------------------------------------- schema


def test_search_code_schema_marks_filters_optional() -> None:
    """#521: `kind`, `file`, `target`, `repo` must be optional in the MCP schema.

    Only `query` and `scope` should be required — everything else is a
    filter that downstream code treats as "no filter" when empty.
    """
    _, schema, _ = _get_search_code()

    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    assert set(schema.get("required", [])) == {"query", "scope"}

    props = schema["properties"]
    for optional in ("kind", "file", "target", "repo"):
        assert optional in props, f"{optional} must be exposed as a param"
        assert optional not in schema["required"], f"{optional} must not be required"


# ---------------------------------------------------------- sentinel coercion


_SENTINEL_CASES = [
    *sorted(_NO_FILTER_SENTINELS),
    *(s.upper() for s in _NO_FILTER_SENTINELS),
    "  -  ",
    " None ",
]


@pytest.mark.parametrize("sentinel", _SENTINEL_CASES)
def test_norm_filter_coerces_sentinels(sentinel: str) -> None:
    """#521 defense-in-depth: placeholder sentinels collapse to ""."""
    assert _norm_filter(sentinel) == ""


@pytest.mark.parametrize("value", ["", None])
def test_norm_filter_preserves_empty(value: Any) -> None:
    assert _norm_filter(value) == ""


def test_norm_filter_preserves_real_values() -> None:
    assert _norm_filter("class") == "class"
    assert _norm_filter("  path/to/file.py  ") == "path/to/file.py"


# ------------------------------------------------------ handler integration


def test_search_code_handler_coerces_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler must strip sentinel filters before hitting the graph.

    Without this, a caller that supplies ``kind='-'`` would be treated as
    filtering to nodes whose kind literally equals ``"-"`` — always zero.
    """
    captured: dict[str, Any] = {}

    class _FakeGraph:
        def search_code(self, **kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return [{"name": "CodeGraph", "kind": "class", "file": "x.py", "line": 1}]

    monkeypatch.setattr(
        "maki_common.tools.codegraph_tools._get_or_build_graph",
        lambda _path, languages=None: _FakeGraph(),
    )

    _, _, handler = _get_search_code()

    asyncio.run(
        handler(
            {
                "query": "CodeGraph",
                "scope": "symbol",
                "kind": "-",
                "file": "none",
                "target": "any",
                "repo": "maki",
            }
        )
    )

    # Every sentinel must have collapsed to "" by the time the graph sees it.
    assert captured["kind"] == ""
    assert captured["file"] == ""
    assert captured["target"] == ""
    # Real params must survive untouched.
    assert captured["query"] == "CodeGraph"
    assert captured["scope"] == "symbol"


def test_search_code_handler_defaults_missing_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitted optional params must default to "" (unfiltered), not KeyError."""
    captured: dict[str, Any] = {}

    class _FakeGraph:
        def search_code(self, **kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return []

    monkeypatch.setattr(
        "maki_common.tools.codegraph_tools._get_or_build_graph",
        lambda _path, languages=None: _FakeGraph(),
    )

    _, _, handler = _get_search_code()

    result = asyncio.run(handler({"query": "CodeGraph", "scope": "symbol"}))

    assert captured["kind"] == ""
    assert captured["file"] == ""
    assert captured["target"] == ""
    # Handler still returned a well-formed MCP result even with no hits.
    assert result.get("content")
