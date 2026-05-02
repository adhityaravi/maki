"""CodeGraph MCP tools — efficient code structure search via tree-sitter.

The graph is built per-repo and cached by workspace path so search_code can
target any registered repo via the optional `repo` arg.
"""

from __future__ import annotations

import logging
from typing import Any

from maki_common.repo import RepoRegistry
from maki_common.tools.utils import mcp_result

log = logging.getLogger(__name__)

# Per-repo graph cache: keyed by absolute workspace path.
_graphs: dict[str, Any] = {}


def invalidate_graph_cache(repo_path: str | None = None) -> None:
    """Drop the cached graph for `repo_path` (or every repo if None).

    Call this after pulling or rewriting files on disk so the next
    `search_code` rebuilds against the new state.
    """
    if repo_path is None:
        _graphs.clear()
    else:
        _graphs.pop(repo_path, None)


def _get_or_build_graph(repo_path: str, languages: list[str] | None = None):
    """Get the cached graph for this repo or build a new one."""
    cached = _graphs.get(repo_path)
    if cached is not None:
        return cached

    from maki_common.codegraph import CodeGraph

    graph = CodeGraph(root=repo_path, languages=languages or ["python"])
    graph.build()
    _graphs[repo_path] = graph
    return graph


async def _resolve_path(registry: RepoRegistry, args: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve the `repo` arg to a workspace path. Returns (path, error)."""
    repo_key = (args.get("repo") or "").strip() or None
    entry = await registry.resolve(repo_key)
    if entry is None:
        if repo_key:
            return None, (
                f"Error: unknown or unreachable repo '{repo_key}'. "
                f"Pass 'owner/name' to clone on demand. Known: {', '.join(registry.known()) or '(none)'}"
            )
        return None, "Error: no default repo registered for this server."
    return entry.path, None


def make_codegraph_tools(
    registry: RepoRegistry,
) -> list[tuple[str, str, dict[str, type], Any]]:
    """Return (name, description, params, handler) tuples for CodeGraph tools.

    Args:
        registry: Multi-repo workspace registry. Each tool's `repo` arg picks
            a workspace; omitted/empty falls back to the default repo.
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
            extra={"query": query, "scope": scope, "kind": kind, "file": file, "repo": args.get("repo")},
        )
        repo_path, err = await _resolve_path(registry, args)
        if repo_path is None:
            return mcp_result(err or "")
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
        log.info("Tool: rebuild_code_graph", extra={"languages": languages, "repo": args.get("repo")})
        repo_path, err = await _resolve_path(registry, args)
        if repo_path is None:
            return mcp_result(err or "")
        try:
            lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()] if languages else None

            from maki_common.codegraph import CodeGraph

            graph = CodeGraph(root=repo_path, languages=lang_list or ["python"])
            graph.build()
            _graphs[repo_path] = graph

            node_count = len(graph._nodes)
            edge_count = graph._edge_count()
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
            "Much faster than reading entire files — use this first to find what you need. "
            "Optional `repo` arg (e.g. 'owner/name' or short name) selects a non-default repo.",
            {"query": str, "scope": str, "kind": str, "file": str, "target": str, "repo": str},
            search_code,
        ),
        (
            "rebuild_code_graph",
            "Rebuild the code structure graph after making code changes or pulling updates. "
            "Optionally specify languages (comma-separated, e.g. 'python,go'). Defaults to Python. "
            "Optional `repo` arg selects a non-default repo.",
            {"languages": str, "repo": str},
            rebuild_code_graph,
        ),
    ]
