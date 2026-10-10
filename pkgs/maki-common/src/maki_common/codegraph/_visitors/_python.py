"""Python language visitor using tree-sitter queries.

Rather than iterate ``node.children`` and dispatch on a hand-picked allow-list
of node types, this visitor runs a few S-expression queries against the parsed
tree and lets tree-sitter walk it for us. That kills a whole family of
"we only looked at direct children" bugs (nested defs, control-flow blocks,
assignment-bound calls, chained/nested-arg calls, aliased imports, etc.) —
each fix used to require another ``elif child.type == …`` branch; here it is
either free (queries find the node anywhere) or one query clause.

Design:

- **Definitions** (``function_definition`` / ``class_definition``): one query
  finds every one, at any nesting depth. Enclosing lexical scope is recovered
  by walking ``node.parent`` up to the nearest enclosing def; the id string
  ``parent_id::name`` is built outer-first so children see their parent's id.
- **Calls**: one query captures every ``call`` expression's target. The
  ``calls`` edge is attributed to the nearest enclosing def (or the module),
  found via the same parent walk. Nested-arg calls (``bar(foo())``) and
  chained calls (``foo().bar()``) both come out because the query engine
  visits them regardless of what wraps them.
- **Decorators**: one query per decorator; owner is the def wrapped by its
  ``decorated_definition`` parent.
- **Imports**: ``import_statement`` / ``import_from_statement`` /
  ``future_import_statement``. Uses field names (``module_name``, ``name``)
  to enumerate imported modules and symbols, so ``import X as Y`` and
  ``from X import a, b as c`` produce full edges.
- **Inheritance**: read the ``superclasses`` field of each class.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Query, QueryCursor
from tree_sitter import Node as TSNode

from maki_common.codegraph._models import Edge, Node
from maki_common.codegraph._visitors._utils import text

PY_LANGUAGE = Language(tspython.language())


# ---------------------------------------------------------------------------
# Queries — module-level so tree-sitter only compiles them once per process.
# ---------------------------------------------------------------------------

# Every def, anywhere. Captures the def node and its name.
_DEF_QUERY = Query(
    PY_LANGUAGE,
    """
    (function_definition
      name: (identifier) @def.name) @def.function

    (class_definition
      name: (identifier) @def.name) @def.class
    """,
)

# Every call expression's function target, anywhere. This alone catches
# top-level assignment-bound calls (#309), calls inside argument lists (#315),
# chained call inner calls (#315), calls inside control-flow blocks (#247).
_CALL_QUERY = Query(
    PY_LANGUAGE,
    """
    (call
      function: [(identifier) (attribute)] @call.target) @call.node
    """,
)

# Every decorator, anywhere.
_DECORATOR_QUERY = Query(
    PY_LANGUAGE,
    """
    (decorator) @decorator.node
    """,
)

# Every import kind, anywhere.
_IMPORT_QUERY = Query(
    PY_LANGUAGE,
    """
    (import_statement) @import.stmt
    (import_from_statement) @import.from
    (future_import_statement) @import.future
    """,
)


class PythonVisitor:
    """Extract nodes and edges from Python source files."""

    extensions: list[str] = [".py"]

    def __init__(self) -> None:
        self._parser = Parser(PY_LANGUAGE)

    def parse_file(self, path: Path, source: str, relative_path: str) -> tuple[list[Node], list[Edge]]:
        """Parse a Python file and extract structural nodes and edges."""
        tree = self._parser.parse(source.encode())
        root = tree.root_node

        module_id = relative_path
        nodes: list[Node] = [
            Node(
                id=module_id,
                kind="module",
                name=Path(relative_path).stem,
                file=relative_path,
                line=1,
                end_line=source.count("\n") + 1,
            )
        ]
        edges: list[Edge] = []

        # ---- Pass 1: definitions ------------------------------------------
        # Collect every function/class definition. We visit them outer-first
        # (start_byte ascending, end_byte descending) so that when we compute
        # a def's id string we can look up its enclosing def's id.
        def_id_by_ts: dict[int, str] = {}
        def_matches = _query_matches(_DEF_QUERY, root)
        def_ts_nodes: list[TSNode] = []
        for _pattern_index, caps in def_matches:
            for key in ("def.function", "def.class"):
                for n in caps.get(key, []):
                    def_ts_nodes.append(n)
        def_ts_nodes.sort(key=lambda n: (n.start_byte, -n.end_byte))
        for def_node in def_ts_nodes:
            self._extract_def(def_node, module_id, def_id_by_ts, relative_path, nodes, edges)

        # ---- Pass 2: calls (attributed to the nearest enclosing scope) ----
        for _pattern_index, caps in _query_matches(_CALL_QUERY, root):
            call_nodes = caps.get("call.node", [])
            target_nodes = caps.get("call.target", [])
            if not call_nodes or not target_nodes:
                continue
            call_node = call_nodes[0]
            target_node = target_nodes[0]
            owner_id = _enclosing_scope_id(call_node, def_id_by_ts, module_id)
            edges.append(
                Edge(
                    source=owner_id,
                    target=text(target_node),
                    kind="calls",
                    line=call_node.start_point[0] + 1,
                )
            )

        # ---- Pass 3: decorators (owner = the def they wrap) ---------------
        for _pattern_index, caps in _query_matches(_DECORATOR_QUERY, root):
            for dec in caps.get("decorator.node", []):
                self._extract_decorator(dec, def_id_by_ts, module_id, edges)

        # ---- Pass 4: imports ----------------------------------------------
        for _pattern_index, caps in _query_matches(_IMPORT_QUERY, root):
            for imp in caps.get("import.stmt", []):
                self._extract_import(imp, def_id_by_ts, module_id, edges)
            for imp in caps.get("import.from", []):
                self._extract_from_import(imp, def_id_by_ts, module_id, edges)
            for imp in caps.get("import.future", []):
                owner_id = _enclosing_scope_id(imp, def_id_by_ts, module_id)
                edges.append(
                    Edge(
                        source=owner_id,
                        target="__future__",
                        kind="imports",
                        line=imp.start_point[0] + 1,
                    )
                )

        return nodes, edges

    # ------------------------------------------------------------------ defs

    def _extract_def(
        self,
        node: TSNode,
        module_id: str,
        def_id_by_ts: dict[int, str],
        relative_path: str,
        nodes: list[Node],
        edges: list[Edge],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return

        parent_id = _enclosing_scope_id(node, def_id_by_ts, module_id)
        name = text(name_node)
        node_id = f"{parent_id}::{name}"
        def_id_by_ts[node.id] = node_id

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        if node.type == "function_definition":
            params = node.child_by_field_name("parameters")
            params_text = text(params) if params else "()"
            # ``async`` is a leading keyword child, not a field.
            is_async = any(ch.type == "async" for ch in node.children)
            return_type_node = node.child_by_field_name("return_type")
            return_type = f" -> {text(return_type_node)}" if return_type_node else ""
            prefix = "async def" if is_async else "def"
            signature = f"{prefix} {name}{params_text}{return_type}"

            nodes.append(
                Node(
                    id=node_id,
                    kind="function",
                    name=name,
                    file=relative_path,
                    line=start_line,
                    end_line=end_line,
                    signature=signature,
                    docstring=_extract_docstring(node),
                    parent=parent_id,
                )
            )
            edges.append(Edge(source=parent_id, target=node_id, kind="contains", line=start_line))

        elif node.type == "class_definition":
            nodes.append(
                Node(
                    id=node_id,
                    kind="class",
                    name=name,
                    file=relative_path,
                    line=start_line,
                    end_line=end_line,
                    docstring=_extract_docstring(node),
                    parent=parent_id,
                )
            )
            edges.append(Edge(source=parent_id, target=node_id, kind="contains", line=start_line))

            superclasses = node.child_by_field_name("superclasses")
            if superclasses is not None:
                for base in superclasses.children:
                    # Accept identifier (Foo) and attribute (pkg.Foo). Other
                    # shapes (subscript for Generic[T], keyword args like
                    # metaclass=…) are intentionally skipped — they aren't
                    # base classes.
                    if base.type in ("identifier", "attribute"):
                        edges.append(
                            Edge(
                                source=node_id,
                                target=text(base),
                                kind="inherits",
                                line=base.start_point[0] + 1,
                            )
                        )

    # ------------------------------------------------------------ decorators

    def _extract_decorator(
        self,
        node: TSNode,
        def_id_by_ts: dict[int, str],
        module_id: str,
        edges: list[Edge],
    ) -> None:
        """Emit a ``calls`` edge for a decorator, owned by the wrapped def."""
        owner_id = _decorated_owner_id(node, def_id_by_ts, module_id)

        # First non-`@` child is the decorator expression.
        expr: TSNode | None = None
        for ch in node.children:
            if ch.type == "@":
                continue
            expr = ch
            break
        if expr is None:
            return

        if expr.type == "call":
            func = expr.child_by_field_name("function")
            if func is None or func.type not in ("identifier", "attribute"):
                return
            target = text(func)
        elif expr.type in ("identifier", "attribute"):
            target = text(expr)
        else:
            return

        edges.append(
            Edge(
                source=owner_id,
                target=target,
                kind="calls",
                line=node.start_point[0] + 1,
            )
        )

    # --------------------------------------------------------------- imports

    def _extract_import(
        self,
        node: TSNode,
        def_id_by_ts: dict[int, str],
        module_id: str,
        edges: list[Edge],
    ) -> None:
        """Extract ``import foo``, ``import foo as f``, ``import a, b``."""
        owner_id = _enclosing_scope_id(node, def_id_by_ts, module_id)
        line = node.start_point[0] + 1
        for name_node in node.children_by_field_name("name"):
            module_name = _import_module_name(name_node)
            if module_name:
                edges.append(Edge(source=owner_id, target=module_name, kind="imports", line=line))

    def _extract_from_import(
        self,
        node: TSNode,
        def_id_by_ts: dict[int, str],
        module_id: str,
        edges: list[Edge],
    ) -> None:
        """Extract ``from X import a, b as c`` statements.

        Emits both:
          - one ``imports`` edge for the module ``X`` (preserves legacy shape)
          - one ``imports`` edge per imported name, targeting ``X::name`` when
            ``X`` is a real dotted module so the resolver can eventually bind
            unqualified callers (#344).
        """
        owner_id = _enclosing_scope_id(node, def_id_by_ts, module_id)
        line = node.start_point[0] + 1

        module_node = node.child_by_field_name("module_name")
        module_str = text(module_node) if module_node else ""
        if module_str:
            edges.append(Edge(source=owner_id, target=module_str, kind="imports", line=line))

        # Relative imports (from . import x) have no useful module prefix to
        # qualify names with — emit just the bare name.
        qualify = bool(module_str) and not module_str.startswith(".")

        for name_node in node.children_by_field_name("name"):
            name_str = _import_module_name(name_node)
            if not name_str:
                continue
            target = f"{module_str}::{name_str}" if qualify else name_str
            edges.append(Edge(source=owner_id, target=target, kind="imports", line=line))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query_matches(query: Query, root: TSNode) -> list[tuple[int, dict[str, list[TSNode]]]]:
    """Run a query and return its matches as (pattern_index, captures) pairs."""
    return QueryCursor(query).matches(root)


def _enclosing_scope_id(node: TSNode, def_id_by_ts: dict[int, str], module_id: str) -> str:
    """Return the id of the nearest enclosing def, or the module id."""
    parent = node.parent
    while parent is not None:
        if parent.type in ("function_definition", "class_definition"):
            found = def_id_by_ts.get(parent.id)
            if found is not None:
                return found
        parent = parent.parent
    return module_id


def _decorated_owner_id(decorator: TSNode, def_id_by_ts: dict[int, str], module_id: str) -> str:
    """Return the id of the def that a decorator wraps.

    A decorator lives inside a ``decorated_definition`` whose siblings include
    one ``function_definition`` or ``class_definition``. If for some reason
    the decorator is orphaned (grammar edge case, error recovery), fall back
    to the nearest enclosing scope so we still emit *some* edge.
    """
    parent = decorator.parent
    if parent is not None and parent.type == "decorated_definition":
        for ch in parent.children:
            if ch.type in ("function_definition", "class_definition"):
                found = def_id_by_ts.get(ch.id)
                if found is not None:
                    return found
    return _enclosing_scope_id(decorator, def_id_by_ts, module_id)


def _import_module_name(name_node: TSNode) -> str:
    """Extract a dotted module name from one entry of an import statement.

    Handles the four shapes that can appear as an import list entry:
    ``dotted_name`` (bare), ``aliased_import`` (``foo as bar``),
    ``relative_import`` (``.``, ``..pkg``), and bare ``identifier``.
    """
    if name_node.type == "dotted_name":
        return text(name_node)
    if name_node.type == "aliased_import":
        inner = name_node.child_by_field_name("name")
        return text(inner) if inner else ""
    if name_node.type == "relative_import":
        return text(name_node)
    if name_node.type == "identifier":
        return text(name_node)
    return ""


def _extract_docstring(node: TSNode) -> str:
    """Extract the docstring literal from a function/class body, if any."""
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return ""
    first = body.children[0]
    if first.type != "expression_statement" or not first.children:
        return ""
    expr = first.children[0]
    if expr.type != "string":
        return ""
    raw = text(expr)
    for q in ('"""', "'''"):
        if raw.startswith(q) and raw.endswith(q):
            return raw[3:-3].strip()
    for q in ('"', "'"):
        if raw.startswith(q) and raw.endswith(q):
            return raw[1:-1].strip()
    return raw
