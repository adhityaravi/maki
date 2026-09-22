"""Tests for CodeGraph.search_code query semantics.

Regression coverage for issue #646 — the default ``symbol`` scope used to
match the query substring against the fully-qualified ``node_id`` in
addition to ``node.name``. Because node IDs embed the file path (e.g.
``pkgs/maki-recall/src/maki_recall/main.py::init_task``), a query like
``"recall"``, ``"config"``, or ``"graph"`` would silently pull in every
symbol in every containing file and — compounded with the hardcoded
50-result cap — push real matches off the end.
"""

from __future__ import annotations

from pathlib import Path

from maki_common.codegraph._graph import CodeGraph
from maki_common.codegraph._models import Node


def _make_graph_with_nodes(nodes: list[Node]) -> CodeGraph:
    """Build a CodeGraph with pre-seeded nodes, skipping filesystem parsing."""
    g = CodeGraph(root=Path("/tmp"), visitors=[])
    for node in nodes:
        g._nodes[node.id] = node
        g._file_index.setdefault(node.file, []).append(node.id)
        g._name_index.setdefault(node.name, []).append(node.id)
    g._built = True  # bypass build() on first search_code call
    return g


# ----------------------------------------------------------------- #646 --


def test_symbol_search_does_not_match_path_substrings() -> None:
    """#646: a query that appears only in the file path must not match.

    ``init_task`` lives in ``pkgs/maki-recall/...`` — a query of ``"recall"``
    used to return it because the path substring matched ``node_id``. It
    should now be filtered out; only symbols whose ``name`` contains
    ``recall`` are returned.
    """
    nodes = [
        Node(
            id="pkgs/maki-recall/src/maki_recall/main.py::init_task",
            kind="function",
            name="init_task",
            file="pkgs/maki-recall/src/maki_recall/main.py",
            line=10,
        ),
        Node(
            id="pkgs/maki-recall/src/maki_recall/main.py::helper",
            kind="function",
            name="helper",
            file="pkgs/maki-recall/src/maki_recall/main.py",
            line=20,
        ),
        Node(
            id="pkgs/maki-stem/src/maki_stem/recall_client.py::RecallClient",
            kind="class",
            name="RecallClient",
            file="pkgs/maki-stem/src/maki_stem/recall_client.py",
            line=5,
        ),
    ]
    g = _make_graph_with_nodes(nodes)

    hits = g.search_code(query="recall", scope="symbol")
    names = {h["name"] for h in hits}

    # Only symbols whose *name* contains "recall" (case-insensitive) come back.
    assert names == {"RecallClient"}
    # Path-only matches are excluded.
    assert "init_task" not in names
    assert "helper" not in names


def test_symbol_search_matches_name_case_insensitively() -> None:
    """#646: substring match on name is case-insensitive (unchanged)."""
    nodes = [
        Node(
            id="pkg/foo.py::CodeGraph",
            kind="class",
            name="CodeGraph",
            file="pkg/foo.py",
            line=1,
        ),
    ]
    g = _make_graph_with_nodes(nodes)

    hits = g.search_code(query="codegraph", scope="symbol")
    assert [h["name"] for h in hits] == ["CodeGraph"]


def test_symbol_search_exact_relevance_is_case_insensitive() -> None:
    """#646: `CodeGraph` should rank as exact for query `codegraph`.

    Previously the exact tier compared ``node.name == query`` raw, so
    casing mismatches silently demoted real exact matches to substring
    relevance. The fix folds both sides to lowercase for the tier check.
    """
    nodes = [
        Node(
            id="pkg/foo.py::CodeGraph",
            kind="class",
            name="CodeGraph",
            file="pkg/foo.py",
            line=1,
        ),
        Node(
            id="pkg/bar.py::CodeGraphBuilder",
            kind="class",
            name="CodeGraphBuilder",
            file="pkg/bar.py",
            line=1,
        ),
    ]
    g = _make_graph_with_nodes(nodes)

    hits = g.search_code(query="codegraph", scope="symbol")
    # Exact match (case-folded) sorts first.
    assert hits[0]["name"] == "CodeGraph"
    assert hits[0]["relevance"] == "exact"
    assert hits[1]["name"] == "CodeGraphBuilder"
    assert hits[1]["relevance"] == "substring"


def test_symbol_search_path_scoped_via_file_filter() -> None:
    """#646: path-scoped search must go through the explicit ``file=`` filter,
    not through smuggling a path token into ``query``.
    """
    nodes = [
        Node(
            id="pkgs/maki-recall/src/maki_recall/main.py::init_task",
            kind="function",
            name="init_task",
            file="pkgs/maki-recall/src/maki_recall/main.py",
            line=10,
        ),
        Node(
            id="pkgs/maki-recall/src/maki_recall/main.py::helper",
            kind="function",
            name="helper",
            file="pkgs/maki-recall/src/maki_recall/main.py",
            line=20,
        ),
        Node(
            id="pkgs/maki-stem/src/maki_stem/main.py::init_task",
            kind="function",
            name="init_task",
            file="pkgs/maki-stem/src/maki_stem/main.py",
            line=10,
        ),
    ]
    g = _make_graph_with_nodes(nodes)

    hits = g.search_code(
        query="",
        scope="symbol",
        file="pkgs/maki-recall/src/maki_recall/main.py",
    )
    files = {h["file"] for h in hits}
    assert files == {"pkgs/maki-recall/src/maki_recall/main.py"}
    assert len(hits) == 2
