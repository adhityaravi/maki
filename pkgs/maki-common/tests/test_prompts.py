"""Tests for the shared prompt-section formatters in ``maki_common.prompts``.

These three helpers replace three near-identical copies that lived in
cortex and the stem loops. The tests pin the exact wire format —
prefix, separators, header strings — so a future refactor cannot silently
shift what the model sees.
"""

# ruff: noqa: I001 — single-import block from this package's own source tree;
# ruff isort flags it spuriously, presumably because the tests/ directory has
# no neighbouring stdlib/third-party imports to anchor a sort order against.
from maki_common.prompts import (
    format_graph_block,
    format_memories_block,
    format_system_state_lines,
)


# --- format_system_state_lines ----------------------------------------------


def test_format_system_state_lines_renders_dict_entries() -> None:
    state = {
        "nats": {"connected": True},
        "cortex": {"healthy": True, "restart_count": 0},
    }
    assert format_system_state_lines(state) == [
        "- nats: connected=True",
        "- cortex: healthy=True, restart_count=0",
    ]


def test_format_system_state_lines_skips_non_dict_entries() -> None:
    state = {
        "ok": {"healthy": True},
        "scalar": 42,
        "string": "hello",
        "none": None,
    }
    assert format_system_state_lines(state) == ["- ok: healthy=True"]


def test_format_system_state_lines_empty() -> None:
    assert format_system_state_lines({}) == []
    assert format_system_state_lines({"scalar": 1}) == []


# --- format_memories_block ---------------------------------------------------


def test_format_memories_block_renders_header_and_bullets() -> None:
    memories = [
        {"text": "Adi likes pour-over coffee", "relevance": 0.92},
        {"text": "We picked Postgres for the memory store", "relevance": 0.81},
    ]
    assert format_memories_block(memories) == (
        "## Relevant memories\n"
        "- Adi likes pour-over coffee (relevance: 0.92)\n"
        "- We picked Postgres for the memory store (relevance: 0.81)"
    )


def test_format_memories_block_missing_relevance_falls_back_to_question_mark() -> None:
    memories = [{"text": "no relevance attached"}]
    assert format_memories_block(memories) == ("## Relevant memories\n- no relevance attached (relevance: ?)")


def test_format_memories_block_empty_returns_none() -> None:
    assert format_memories_block([]) is None


# --- format_graph_block ------------------------------------------------------


def test_format_graph_block_renders_header_and_bullets() -> None:
    graph = ["Maki -[RUNS_ON]-> NUC", "Maki -[REMEMBERS]-> Adi"]
    assert format_graph_block(graph) == ("## Relationships\n- Maki -[RUNS_ON]-> NUC\n- Maki -[REMEMBERS]-> Adi")


def test_format_graph_block_empty_returns_none() -> None:
    assert format_graph_block([]) is None
