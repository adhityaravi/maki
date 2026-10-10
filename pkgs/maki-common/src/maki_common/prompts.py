"""Shared prompt-section formatters used by cortex and the stem loops.

Three small helpers — system_state lines, memories block, graph block —
that were re-implemented in cortex's ``build_system_prompt`` and in the
stem loop prompt builders. Keeping them in one place guarantees the
headers and relevance-rendering stay in sync across services.

Each helper returns either a list of lines (for the system_state
formatter, which leaves the header/joiner to the caller) or a fully
rendered ``"## Section\\n- ...\\n- ..."`` block, or ``None`` when there
is nothing to render. Callers stitch the non-None blocks together with
``"\\n\\n".join(...)``.
"""

from __future__ import annotations


def format_system_state_lines(state: dict) -> list[str]:
    """Return ``"- name: k=v, k=v"`` lines for each dict-valued entry in *state*.

    The caller decides the section header and the joiner — typically
    ``"## Your system state\\n" + "\\n".join(lines)``. Non-dict entries
    are skipped (matches the long-standing behaviour of the inline
    callers we're replacing). Returns an empty list when no dict
    entries are present, so callers can branch on truthiness:

        lines = format_system_state_lines(state)
        if lines:
            parts.append("## Your system state\\n" + "\\n".join(lines))
    """
    lines: list[str] = []
    for name, info in state.items():
        if isinstance(info, dict):
            details = ", ".join(f"{k}={v}" for k, v in info.items())
            lines.append(f"- {name}: {details}")
    return lines


def format_memories_block(memories: list[dict]) -> str | None:
    """Return a ``"## Relevant memories\\n- ..."`` block or ``None`` if empty.

    Each memory is rendered as ``"- {text} (relevance: {relevance})"``.
    Missing ``relevance`` falls back to ``"?"``. The ``text`` key is
    required — callers passing other shapes will get a ``KeyError``,
    matching what the inline copies did before.
    """
    if not memories:
        return None
    lines = [f"- {m['text']} (relevance: {m.get('relevance', '?')})" for m in memories]
    return "## Relevant memories\n" + "\n".join(lines)


def format_graph_block(graph: list[str]) -> str | None:
    """Return a ``"## Relationships\\n- ..."`` block or ``None`` if empty.

    Each entry is a pre-formatted relationship string from the
    knowledge-graph fetcher; we just bullet them under a fixed header.
    """
    if not graph:
        return None
    return "## Relationships\n" + "\n".join(f"- {r}" for r in graph)
