"""CodeGraph MCP tools — efficient code structure search via tree-sitter."""

from __future__ import annotations

import logging
from typing import Any

from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

# Module-level graph cache
_graph = None
_graph_repo_path = None


def _get_or_build_graph(repo_path: str, languages: list[str] | None = None):
    """Get the cached graph or build a new one."""
    global _graph, _graph_repo_path

    if _graph is not None and _graph_repo_path == repo_path:
        return _graph

    from maki_common.codegraph import CodeGraph

    _graph = CodeGraph(root=repo_path, languages=languages or ["python"])
    _graph.build()
    _graph_repo_path = repo_path
    return _graph


def make_codegraph_tools(
    repo_path: str,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for CodeGraph tools.

    Args:
        repo_path: Absolute path to the local git repo clone.
    """

    async def search_code(args: dict[str, Any]) -> dict[str, Any]:
        """Search the code structure graph for symbols, callers, callees, etc."""
        query = args.get("query", "")
        scope = args.get("scope", "symbol")
        kind = args.get("kind", "")
        file = args.get("file", "")
        target = args.get("target", "")
        log.info(
            "Tool: search_code",
            extra={"query": query, "scope": scope, "kind": kind, "file": file},
        )
        try:
            graph = _get_or_build_graph(repo_path)
            results = graph.search_code(
                query=query,
                scope=scope,
                kind=kind,
                file=file,
                target=target,
            )
            if not results:
                return mcp_result(f"No results found for query='{query}' scope='{scope}'.")

            # Format compact results
            lines = []
            for r in results:
                name = r.get("name", "")
                rkind = r.get("kind", "")
                rfile = r.get("file", "")
                line_num = r.get("line", 0)
                sig = r.get("signature", "")
                ctx = r.get("context", "")
                doc = r.get("docstring", "")

                parts = [f"{name} ({rkind}) at {rfile}:{line_num}"]
                if sig:
                    parts.append(f"  {sig}")
                if doc:
                    # First line of docstring only
                    parts.append(f'  "{doc.split(chr(10))[0]}"')
                if ctx:
                    parts.append(f"  [{ctx}]")
                lines.append("\n".join(parts))

            header = f"Found {len(results)} result(s) for query='{query}' scope='{scope}':\n"
            return mcp_result(header + "\n".join(lines))
        except Exception as e:
            log.exception("search_code failed")
            return mcp_result(f"Error: {e}")

    async def rebuild_code_graph(args: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the code structure graph (after code changes or pulls)."""
        languages = args.get("languages", "")
        log.info("Tool: rebuild_code_graph", extra={"languages": languages})
        try:
            global _graph, _graph_repo_path

            lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()] if languages else None

            from maki_common.codegraph import CodeGraph

            _graph = CodeGraph(root=repo_path, languages=lang_list or ["python"])
            _graph.build()
            _graph_repo_path = repo_path

            node_count = len(_graph._nodes)
            edge_count = _graph._edge_count()
            return mcp_result(f"Code graph rebuilt: {node_count} symbols, {edge_count} relationships.")
        except Exception as e:
            log.exception("rebuild_code_graph failed")
            return mcp_result(f"Error rebuilding graph: {e}")

    return [
        (
            "search_code",
            "Search the code structure graph. Finds symbols, callers, callees, and relationships "
            "efficiently using tree-sitter AST analysis. "
            "Scopes: symbol (default), callers, callees, references, definition, file, path. "
            "Kinds: function, class, module. "
            "Much faster than reading entire files — use this first to find what you need.",
            {"query": str, "scope": str, "kind": str, "file": str, "target": str},
            search_code,
        ),
        (
            "rebuild_code_graph",
            "Rebuild the code structure graph after making code changes or pulling updates. "
            "Optionally specify languages (comma-separated, e.g. 'python,go'). Defaults to Python.",
            {"languages": str},
            rebuild_code_graph,
        ),
    ]
