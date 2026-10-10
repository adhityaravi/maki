"""Tests for the tag helpers in ``maki_common.config``.

The three helpers (``parse_config_tags``, ``parse_tagged``, ``strip_tags``)
must agree on what a ``[TAG:content]`` looks like. Historically they used
three different regex shapes — ``parse_tagged`` matched multi-line content
via ``re.DOTALL`` while ``strip_tags`` stopped at the first newline, which
meant multi-line tags could be *parsed* but never *stripped*. That silently
leaked immune's multi-paragraph ``[DIGEST:...]`` / ``[ALERT:...]`` payloads
through any code path that expected ``strip_tags`` to be the inverse of
``parse_tagged``. See issue #136.

A second variant of the same class of bug: even after unification on the
lazy-regex shape ``\\[(\\w+):(.*?)\\]``, the ``.*?`` closes at the FIRST
``]`` inside content — silently truncating payloads with type annotations
(``list[str]``), markdown links (``[runbook](url)``), issue refs
(``[#123]``), and code fragments Claude quotes back into DIGEST/ALERT.
See issue #505; now fixed by switching to a balanced-bracket scanner.

These tests pin the shared contract so both classes of drift cannot come back.
"""

# ruff: noqa: I001 — single-import block from this package's own source tree.
from maki_common.config import parse_config_tags, parse_tagged, strip_tags


# --- parse_tagged ------------------------------------------------------------


def test_parse_tagged_single_line() -> None:
    assert parse_tagged("[ALERT:something is on fire]", "ALERT") == ["something is on fire"]


def test_parse_tagged_multi_line() -> None:
    text = "prefix [ALERT:line1\nline2\nline3] suffix"
    assert parse_tagged(text, "ALERT") == ["line1\nline2\nline3"]


def test_parse_tagged_multiple_matches() -> None:
    text = "[DIGEST:first] and [DIGEST:second\nwith newline]"
    assert parse_tagged(text, "DIGEST") == ["first", "second\nwith newline"]


def test_parse_tagged_ignores_other_tags() -> None:
    assert parse_tagged("[ALERT:x] [DIGEST:y]", "ALERT") == ["x"]


def test_parse_tagged_no_match_returns_empty() -> None:
    assert parse_tagged("no tags here", "ALERT") == []


# --- parse_tagged: internal ``]`` (issue #505) -------------------------------


def test_parse_tagged_preserves_type_annotation_in_content() -> None:
    """Regression for #505: ``list[str]`` inside content was truncated at
    the first ``]`` under the old lazy regex. Balanced-bracket scanner must
    now preserve it."""
    text = "[DIGEST:cortex stuck processing list[str] payloads]"
    assert parse_tagged(text, "DIGEST") == ["cortex stuck processing list[str] payloads"]


def test_parse_tagged_preserves_markdown_link_in_content() -> None:
    """Regression for #505: a markdown link ``[runbook](url)`` inside content
    used to leave ``(url) for details]`` leaking into strip_tags output."""
    text = "[DIGEST:see [runbook](url) for details]"
    assert parse_tagged(text, "DIGEST") == ["see [runbook](url) for details"]


def test_parse_tagged_preserves_issue_ref_in_content() -> None:
    """Regression for #505: ``[#123]`` issue refs inside ALERT payloads."""
    text = "[ALERT:regression from [#123]]"
    assert parse_tagged(text, "ALERT") == ["regression from [#123]"]


def test_parse_tagged_preserves_nested_brackets_across_multiple_tags() -> None:
    """Two tags side by side, each with internal ``]`` — both must survive."""
    text = "[DIGEST:cortex on list[str]] and [ALERT:see [runbook](url)]"
    assert parse_tagged(text, "DIGEST") == ["cortex on list[str]"]
    assert parse_tagged(text, "ALERT") == ["see [runbook](url)"]


def test_parse_tagged_unbalanced_opener_is_skipped() -> None:
    """A tag with no closing ``]`` isn't a tag. Confirm we don't hang or
    swallow a *later* well-formed tag when an earlier opener is bad."""
    text = "[DIGEST:no closer here and then [ALERT:this one closes]"
    # The DIGEST opener never closes, so it isn't emitted. The ALERT nested
    # within is still parsed (start-scan resumes after the bad opener).
    assert parse_tagged(text, "DIGEST") == []
    assert parse_tagged(text, "ALERT") == ["this one closes"]


# --- strip_tags --------------------------------------------------------------


def test_strip_tags_removes_single_line_tag() -> None:
    assert strip_tags("hello [CONFIG:k=v] world") == "hello  world".strip()


def test_strip_tags_removes_multi_line_tag() -> None:
    """The bug: strip_tags used ``[^\\]]*`` (single-line only), so any tag
    spanning a newline leaked through untouched. Round-trip with parse_tagged
    must now leave the input clean."""
    text = "before [ALERT:line1\nline2\nline3] after"
    assert strip_tags(text) == "before  after".strip()


def test_strip_tags_removes_all_tag_shapes() -> None:
    text = "[CONFIG:a=1] keep [DIGEST:multi\nline] and [ALERT:x]"
    assert strip_tags(text) == "keep  and"


def test_strip_tags_removes_tag_with_internal_close_bracket() -> None:
    """Regression for #505: strip_tags used to leave the trailing ``] payloads``
    in the summary after the lazy regex closed early."""
    text = "prefix [DIGEST:cortex stuck on list[str] payloads] suffix"
    assert strip_tags(text) == "prefix  suffix".strip()


def test_strip_tags_removes_markdown_link_tag_cleanly() -> None:
    text = "before [DIGEST:see [runbook](url) for details] after"
    assert strip_tags(text) == "before  after".strip()


# --- round-trip contract -----------------------------------------------------


def test_round_trip_multi_line_leaves_input_clean() -> None:
    """This is the regression test for issue #136: whatever parse_tagged
    extracts, strip_tags must remove — including multi-line payloads."""
    content = "line1\nline2\nline3"
    text = f"prefix [ALERT:{content}] suffix"
    assert parse_tagged(text, "ALERT") == [content]
    assert strip_tags(text) == "prefix  suffix".strip()


def test_round_trip_with_internal_close_bracket() -> None:
    """Regression for issue #505: the parse ⇄ strip contract must survive
    payloads with internal ``]``. Otherwise digests with type annotations,
    markdown links, or code fragments leak trailing garbage into Discord."""
    content = "cortex stuck on list[str]; see [runbook](url) — regression from [#123]"
    text = f"prefix [DIGEST:{content}] suffix"
    assert parse_tagged(text, "DIGEST") == [content]
    assert strip_tags(text) == "prefix  suffix".strip()


# --- parse_config_tags -------------------------------------------------------


def test_parse_config_tags_basic() -> None:
    assert parse_config_tags("[CONFIG:foo=bar]") == [("foo", "bar")]


def test_parse_config_tags_multiple() -> None:
    text = "[CONFIG:a=1] noise [CONFIG:b=2]"
    assert parse_config_tags(text) == [("a", "1"), ("b", "2")]


def test_parse_config_tags_ignores_other_tags() -> None:
    assert parse_config_tags("[ALERT:not config] [CONFIG:k=v]") == [("k", "v")]


def test_parse_config_tags_no_equals_is_skipped() -> None:
    assert parse_config_tags("[CONFIG:justakey]") == []


def test_parse_config_tags_preserves_json_array_value() -> None:
    """Regression for issue #505 (and #391): JSON array values contain ``]``
    which the old lazy regex truncated, forcing workarounds like
    comma-separated strings. Balanced-bracket scanner must let arrays through
    intact so ``apply_config_updates`` can ``json.loads`` them."""
    text = '[CONFIG:allowlist=["cortex", "recall"]]'
    assert parse_config_tags(text) == [("allowlist", '["cortex", "recall"]')]


def test_parse_config_tags_preserves_json_object_value() -> None:
    """JSON objects use ``{}`` but string values inside them may contain ``]``;
    the balanced scanner ignores braces so this Just Works — pin it anyway."""
    text = '[CONFIG:limits={"a": 1, "b": 2}]'
    assert parse_config_tags(text) == [("limits", '{"a": 1, "b": 2}')]
