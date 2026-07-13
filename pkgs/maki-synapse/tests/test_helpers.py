"""Unit tests for the zero-dependency helpers in ``maki_synapse.main``.

Pins the contracts of the four pure functions that are exercised on every
synapse turn but that previously had no direct coverage:

  - ``build_tool_prompt`` — OpenAI→Claude tool-prompt header assembly
  - ``_serialize_messages`` — role flattening into (system_parts, user_parts)
  - ``extract_json_str`` — markdown-fence + object/array extraction
  - ``_map_usage`` — Claude TokenUsage → OpenAI Usage projection

These helpers are pure text/data — no FastAPI, NATS, or live Claude required.
"""

# ruff: noqa: I001 — single-import block from this package's own source tree;
# ruff isort flags it spuriously, see pkgs/maki-common/tests/test_prompts.py.
import json

from fastapi import HTTPException
from maki_common.claude import TokenUsage
from maki_synapse.main import (
    ChatMessage,
    ToolCallFunction,
    ToolCallItem,
    ToolDefinition,
    ToolFunction,
    Usage,
    _map_usage,
    _serialize_messages,
    build_tool_prompt,
    extract_json_str,
)


# --- build_tool_prompt ------------------------------------------------------


def test_build_tool_prompt_lists_tool_name_and_description() -> None:
    tools = [ToolDefinition(function=ToolFunction(name="search", description="find things"))]
    prompt = build_tool_prompt(tools)
    assert "- search: find things" in prompt
    # The engineered wire format that the parser looks for on the way back.
    assert '{"tool_calls":' in prompt


def test_build_tool_prompt_serializes_parameters_schema_as_json() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    tools = [ToolDefinition(function=ToolFunction(name="search", description="d", parameters=schema))]
    prompt = build_tool_prompt(tools)
    # Parameters must round-trip as JSON so the model sees the actual schema.
    assert f"Parameters schema: {json.dumps(schema)}" in prompt


def test_build_tool_prompt_required_branch_forces_tool_call() -> None:
    """required=True → 'MUST call' language, no plain-text fallback offer."""
    tools = [ToolDefinition(function=ToolFunction(name="x"))]
    prompt = build_tool_prompt(tools, required=True)
    assert "You MUST call one of the tools below" in prompt
    assert "plain text" not in prompt.split("Available tools:", 1)[0]


def test_build_tool_prompt_default_branch_offers_plain_text_fallback() -> None:
    """required=False → plain-text fallback, no MUST-call directive."""
    tools = [ToolDefinition(function=ToolFunction(name="x"))]
    prompt = build_tool_prompt(tools, required=False)
    assert "respond with plain text" in prompt
    assert "MUST call" not in prompt


def test_build_tool_prompt_multiple_tools_all_listed() -> None:
    tools = [
        ToolDefinition(function=ToolFunction(name="a", description="alpha")),
        ToolDefinition(function=ToolFunction(name="b", description="beta")),
    ]
    prompt = build_tool_prompt(tools)
    assert "- a: alpha" in prompt
    assert "- b: beta" in prompt


# --- _serialize_messages ----------------------------------------------------


def test_serialize_messages_system_and_user_split() -> None:
    msgs = [
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hello"),
    ]
    system, user = _serialize_messages(msgs)
    assert system == ["be terse"]
    assert user == ["hello"]


def test_serialize_messages_empty_content_uses_empty_string() -> None:
    """content=None on user/system must not crash — coerce to ''."""
    msgs = [
        ChatMessage(role="system", content=None),
        ChatMessage(role="user", content=None),
    ]
    system, user = _serialize_messages(msgs)
    assert system == [""]
    assert user == [""]


def test_serialize_messages_assistant_content_only() -> None:
    msgs = [ChatMessage(role="assistant", content="prior reply")]
    _, user = _serialize_messages(msgs)
    assert user == ["Assistant: prior reply"]


def test_serialize_messages_assistant_tool_calls_only() -> None:
    """Assistant with tool_calls but no content → only the tool-calls tag."""
    msgs = [
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCallItem(id="c1", function=ToolCallFunction(name="search", arguments='{"q":"cats"}')),
            ],
        ),
    ]
    _, user = _serialize_messages(msgs)
    assert len(user) == 1
    assert user[0].startswith("Assistant: ")
    assert '[Tool calls: search({"q":"cats"})]' in user[0]


def test_serialize_messages_assistant_content_and_tool_calls_combined() -> None:
    """Both content and tool_calls present → both appear in the flattened line."""
    msgs = [
        ChatMessage(
            role="assistant",
            content="Let me search.",
            tool_calls=[
                ToolCallItem(id="c1", function=ToolCallFunction(name="search", arguments="{}")),
            ],
        ),
    ]
    _, user = _serialize_messages(msgs)
    assert "Let me search." in user[0]
    assert "[Tool calls: search({})]" in user[0]


def test_serialize_messages_tool_result_prefers_name_over_id() -> None:
    msgs = [
        ChatMessage(role="tool", name="search", tool_call_id="call_1", content="42 results"),
    ]
    _, user = _serialize_messages(msgs)
    assert user == ["[Tool result from search]: 42 results"]


def test_serialize_messages_tool_result_falls_back_to_tool_call_id() -> None:
    """No name → use tool_call_id as identifier."""
    msgs = [ChatMessage(role="tool", tool_call_id="call_abc", content="ok")]
    _, user = _serialize_messages(msgs)
    assert user == ["[Tool result from call_abc]: ok"]


def test_serialize_messages_tool_result_final_fallback() -> None:
    """Neither name nor tool_call_id → literal 'tool'."""
    msgs = [ChatMessage(role="tool", content="ok")]
    _, user = _serialize_messages(msgs)
    assert user == ["[Tool result from tool]: ok"]


def test_serialize_messages_legacy_function_role_matches_tool() -> None:
    """role='function' (legacy OpenAI) must be treated like role='tool'."""
    msgs = [ChatMessage(role="function", name="lookup", content="x")]
    _, user = _serialize_messages(msgs)
    assert user == ["[Tool result from lookup]: x"]


def test_serialize_messages_unknown_role_raises_400() -> None:
    msgs = [ChatMessage(role="wizard", content="you shall not pass")]
    try:
        _serialize_messages(msgs)
    except HTTPException as e:
        assert e.status_code == 400
        assert "wizard" in e.detail
    else:
        raise AssertionError("expected HTTPException")


def test_serialize_messages_preserves_order_across_roles() -> None:
    """user_parts preserves the interleaved order of user/assistant/tool messages."""
    msgs = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
        ChatMessage(role="user", content="again"),
    ]
    _, user = _serialize_messages(msgs)
    assert user == ["hi", "Assistant: hello", "again"]


# --- extract_json_str -------------------------------------------------------


def test_extract_json_str_markdown_fence_with_language_marker() -> None:
    text = '```json\n{"a": 1}\n```'
    assert extract_json_str(text) == '{"a": 1}'


def test_extract_json_str_markdown_fence_without_language_marker() -> None:
    text = '```\n{"a": 1}\n```'
    assert extract_json_str(text) == '{"a": 1}'


def test_extract_json_str_direct_object_with_preamble() -> None:
    text = 'here is the json: {"a": 1, "b": 2} — enjoy!'
    assert extract_json_str(text) == '{"a": 1, "b": 2}'


def test_extract_json_str_top_level_array() -> None:
    text = "prefix [1, 2, 3] suffix"
    assert extract_json_str(text) == "[1, 2, 3]"


def test_extract_json_str_object_wins_over_array_when_both_present() -> None:
    """Braces are searched before brackets — object extraction takes priority."""
    text = '[1, 2] and then {"k": "v"}'
    assert extract_json_str(text) == '{"k": "v"}'


def test_extract_json_str_no_json_returns_stripped_original() -> None:
    text = "   just prose, no braces here   "
    assert extract_json_str(text) == "just prose, no braces here"


def test_extract_json_str_bare_object_passes_through() -> None:
    text = '{"already": "clean"}'
    assert extract_json_str(text) == '{"already": "clean"}'


def test_extract_json_str_strips_leading_and_trailing_whitespace() -> None:
    text = '\n\n  {"a": 1}  \n\n'
    assert extract_json_str(text) == '{"a": 1}'


# --- _map_usage -------------------------------------------------------------


def test_map_usage_prompt_tokens_include_cache_reads_and_creation() -> None:
    """Regression guard for #107: cache tokens are billed and must be reported."""
    tu = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=100,
        cache_creation_tokens=50,
    )
    usage = _map_usage(tu)
    assert isinstance(usage, Usage)
    assert usage.prompt_tokens == 10 + 100 + 50
    assert usage.completion_tokens == 5
    # total_tokens comes from TokenUsage.total_tokens (input + output), not
    # from our prompt_tokens + completion_tokens sum — pin that behaviour too.
    assert usage.total_tokens == tu.total_tokens
    assert usage.total_tokens == 15


def test_map_usage_zero_tokens_maps_to_zero_usage() -> None:
    usage = _map_usage(TokenUsage())
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0


def test_map_usage_no_cache_activity() -> None:
    """When cache fields are zero, prompt_tokens equals plain input_tokens."""
    tu = TokenUsage(input_tokens=42, output_tokens=7)
    usage = _map_usage(tu)
    assert usage.prompt_tokens == 42
    assert usage.completion_tokens == 7
