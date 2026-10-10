"""Tests for the Python code-graph visitor.

Regression coverage for the seven issues that the tree-sitter query refactor
lands together (#244, #247, #303, #309, #315, #344, #360). Each test names
its issue in a docstring so the intent stays legible if the visitor's
internal shape changes again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

from maki_common.codegraph._visitors._python import PythonVisitor


def _parse(source: str, rel: str = "sample.py"):
    v = PythonVisitor()
    return v.parse_file(Path(rel), source, rel)


def _edges(edges, kind: str, source: str | None = None):
    out = [(e.source, e.target) for e in edges if e.kind == kind]
    if source is not None:
        out = [(s, t) for s, t in out if s == source]
    return out


def _node_ids(nodes) -> set[str]:
    return {n.id for n in nodes}


# ------------------------------------------------------------------- #244 --


def test_nested_defs_registered_as_nodes() -> None:
    """#244: a def inside a def is a real node with a ``contains`` edge."""
    nodes, edges = _parse(
        """
def outer():
    def inner():
        helper()
    inner()
"""
    )
    ids = _node_ids(nodes)
    assert "sample.py::outer" in ids
    assert "sample.py::outer::inner" in ids

    contains = _edges(edges, "contains")
    assert ("sample.py", "sample.py::outer") in contains
    assert ("sample.py::outer", "sample.py::outer::inner") in contains


def test_nested_def_calls_attributed_to_the_nested_def() -> None:
    """#244: ``helper()`` inside ``inner`` is attributed to ``inner``, not ``outer``."""
    _nodes, edges = _parse(
        """
def outer():
    def inner():
        helper()
    inner()
"""
    )
    calls = _edges(edges, "calls")
    assert ("sample.py::outer::inner", "helper") in calls
    # And ``outer`` still gets credit for calling ``inner``.
    assert ("sample.py::outer", "inner") in calls
    # ``outer`` must NOT be credited for calling ``helper``.
    assert ("sample.py::outer", "helper") not in calls


# ------------------------------------------------------------------- #247 --


def test_defs_under_control_flow_are_visible() -> None:
    """#247: a def under ``if`` / ``try`` shows up in the graph."""
    nodes, edges = _parse(
        """
if SOME_FLAG:
    def feature_handler():
        pass

try:
    class Fast:
        pass
except ImportError:
    class Fast:
        pass
"""
    )
    ids = _node_ids(nodes)
    assert "sample.py::feature_handler" in ids
    contains = _edges(edges, "contains")
    assert ("sample.py", "sample.py::feature_handler") in contains


def test_imports_inside_try_except_are_recorded() -> None:
    """#247: import edges inside ``try/except`` fallback blocks aren't lost."""
    _nodes, edges = _parse(
        """
try:
    import ujson as json
except ImportError:
    import json
"""
    )
    imports = _edges(edges, "imports")
    # Both branches emit an ``imports -> ujson`` and ``imports -> json`` edge.
    assert ("sample.py", "ujson") in imports
    assert ("sample.py", "json") in imports


# ------------------------------------------------------------------- #303 --


def test_aliased_import_produces_edge() -> None:
    """#303: ``import numpy as np`` emits ``imports -> numpy``."""
    _nodes, edges = _parse("import numpy as np\n")
    assert ("sample.py", "numpy") in _edges(edges, "imports")


def test_multiple_aliased_imports_on_one_line() -> None:
    """#303: mixed aliased and bare imports on one line all get edges."""
    _nodes, edges = _parse("import a.b as ab, c.d\n")
    imports = _edges(edges, "imports")
    assert ("sample.py", "a.b") in imports
    assert ("sample.py", "c.d") in imports


# ------------------------------------------------------------------- #309 --


def test_module_level_assignment_bound_calls_emit_edges() -> None:
    """#309: ``log = getLogger(__name__)`` at module level emits a call edge."""
    _nodes, edges = _parse("log = getLogger(__name__)\n")
    assert ("sample.py", "getLogger") in _edges(edges, "calls")


def test_class_body_assignment_bound_call_emits_edge() -> None:
    """#309: same, at class body scope, attributed to the class."""
    _nodes, edges = _parse(
        """
class C:
    x = init()
"""
    )
    calls = _edges(edges, "calls")
    assert ("sample.py::C", "init") in calls


# ------------------------------------------------------------------- #315 --


def test_nested_arg_calls_emit_edges() -> None:
    """#315: ``bar(foo())`` emits edges for both ``bar`` and ``foo``."""
    _nodes, edges = _parse(
        """
def caller():
    return bar(foo())
"""
    )
    calls = _edges(edges, "calls")
    assert ("sample.py::caller", "bar") in calls
    assert ("sample.py::caller", "foo") in calls


def test_chained_call_inner_emits_edge() -> None:
    """#315: ``foo().baz()`` emits an edge for the inner ``foo`` too."""
    _nodes, edges = _parse(
        """
def caller():
    return foo().baz()
"""
    )
    calls = _edges(edges, "calls")
    # Inner call: foo
    assert ("sample.py::caller", "foo") in calls
    # Outer call: foo().baz (attribute text). Resolver strips prefix.
    assert any(t.endswith("baz") for s, t in calls if s == "sample.py::caller")


# ------------------------------------------------------------------- #344 --


def test_from_import_emits_per_name_edges() -> None:
    """#344: ``from foo import bar, baz`` records the imported names."""
    _nodes, edges = _parse("from foo import bar, baz\n")
    imports = _edges(edges, "imports")
    # Module edge preserved.
    assert ("sample.py", "foo") in imports
    # Per-name edges emitted with a qualified target.
    assert ("sample.py", "foo::bar") in imports
    assert ("sample.py", "foo::baz") in imports


def test_from_import_with_alias_uses_original_name() -> None:
    """#344/#303: ``from foo import bar as b`` targets ``foo::bar`` (not ``b``)."""
    _nodes, edges = _parse("from foo import bar as b\n")
    imports = _edges(edges, "imports")
    assert ("sample.py", "foo::bar") in imports


def test_from_relative_import_records_names() -> None:
    """#344: ``from . import sibling`` records the name (not qualified)."""
    _nodes, edges = _parse("from . import sibling\n")
    imports = _edges(edges, "imports")
    assert ("sample.py", ".") in imports
    assert ("sample.py", "sibling") in imports


# ------------------------------------------------------------------- #360 --


def test_async_def_signature_includes_async_keyword() -> None:
    """#360: signature for ``async def foo()`` starts with ``async def``."""
    nodes, _edges_ = _parse("async def fetch(url):\n    pass\n")
    fn = next(n for n in nodes if n.name == "fetch")
    assert fn.signature.startswith("async def"), fn.signature


def test_return_type_captured_in_signature() -> None:
    """#360: ``def foo(x: int) -> bytes`` puts ``-> bytes`` in the signature."""
    nodes, _edges_ = _parse("def foo(x: int) -> bytes:\n    return b''\n")
    fn = next(n for n in nodes if n.name == "foo")
    assert "-> bytes" in fn.signature, fn.signature


def test_async_def_with_return_type() -> None:
    """#360: both async and return type are captured together."""
    nodes, _edges_ = _parse("async def foo() -> int:\n    return 1\n")
    fn = next(n for n in nodes if n.name == "foo")
    assert fn.signature.startswith("async def")
    assert "-> int" in fn.signature


def test_plain_def_still_gets_bare_signature() -> None:
    """#360: existing snapshot preserved for sync defs with no return type."""
    nodes, _edges_ = _parse("def foo(x): pass\n")
    fn = next(n for n in nodes if n.name == "foo")
    assert fn.signature == "def foo(x)"


# ---------------------------------------------------------- inheritance ----


def test_qualified_base_class_recorded() -> None:
    """``class D(pkg.A)`` records an ``inherits -> pkg.A`` edge."""
    _nodes, edges = _parse(
        """
class D(pkg.A, B):
    pass
"""
    )
    inherits = _edges(edges, "inherits")
    assert ("sample.py::D", "pkg.A") in inherits
    assert ("sample.py::D", "B") in inherits


# ------------------------------------------------------------ decorators ---


def test_decorator_edges_owned_by_decorated_def() -> None:
    """Decorator calls are attributed to the def they wrap."""
    _nodes, edges = _parse(
        """
@app.get("/x")
@requires_auth
def handler():
    pass
"""
    )
    calls = _edges(edges, "calls", source="sample.py::handler")
    targets = {t for _s, t in calls}
    assert "app.get" in targets
    assert "requires_auth" in targets


# ---------------------------------------------------------------- module ---


def test_module_node_emitted_even_for_empty_file() -> None:
    """An empty file still gets exactly one module node and no edges."""
    nodes, edges = _parse("", rel="empty.py")
    assert [n.id for n in nodes] == ["empty.py"]
    assert edges == []


def test_own_file_import_edge_recovered() -> None:
    """Regression: the visitor's own ``import tree_sitter_python as tspython``
    line used to produce zero edges (#303). It should now be visible."""
    _nodes, edges = _parse(
        "import tree_sitter_python as tspython\n",
        rel="self.py",
    )
    assert ("self.py", "tree_sitter_python") in _edges(edges, "imports")
