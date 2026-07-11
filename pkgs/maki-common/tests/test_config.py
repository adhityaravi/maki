"""Tests for the tag helpers in ``maki_common.config``.

The three helpers (``parse_config_tags``, ``parse_tagged``, ``strip_tags``)
must agree on what a ``[TAG:content]`` looks like. Historically they used
three different regex shapes — ``parse_tagged`` matched multi-line content
via ``re.DOTALL`` while ``strip_tags`` stopped at the first newline, which
meant multi-line tags could be *parsed* but never *stripped*. That silently
leaked immune's multi-paragraph ``[DIGEST:...]`` / ``[ALERT:...]`` payloads
through any code path that expected ``strip_tags`` to be the inverse of
``parse_tagged``. See issue #136.

These tests pin the shared contract so the drift cannot come back.
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


# --- round-trip contract -----------------------------------------------------


def test_round_trip_multi_line_leaves_input_clean() -> None:
    """This is the regression test for issue #136: whatever parse_tagged
    extracts, strip_tags must remove — including multi-line payloads."""
    content = "line1\nline2\nline3"
    text = f"prefix [ALERT:{content}] suffix"
    assert parse_tagged(text, "ALERT") == [content]
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
