"""Unit tests for the chat_completions helpers.

These pin the contracts of the three single-responsibility functions that
chat_completions was split into (see #112). They don't need FastAPI,
NATS, or a live Claude — they exercise the pure-text pieces directly.
"""

# ruff: noqa: I001 — single-import block from this package's own source tree;
# ruff isort flags it spuriously, see pkgs/maki-common/tests/test_prompts.py.
from maki_synapse.main import (
    ChatCompletionRequest,
    ChatMessage,
    ToolDefinition,
    ToolFunction,
    _build_system_and_user,
    _parse_response,
    try_parse_json_lenient,
)


# --- _parse_response --------------------------------------------------------


def test_parse_response_plain_text_without_tools() -> None:
    """No tools registered → text passes through, finish_reason="stop"."""
    msg, finish = _parse_response("just some text", has_tools=False)
    assert msg.content == "just some text"
    assert msg.tool_calls is None
    assert finish == "stop"


def test_parse_response_plain_text_with_tools_falls_back() -> None:
    """has_tools=True but model returned conversational text → text fallback."""
    msg, finish = _parse_response("hello world", has_tools=True)
    assert msg.content == "hello world"
    assert msg.tool_calls is None
    assert finish == "stop"


def test_parse_response_valid_tool_calls() -> None:
    text = '{"tool_calls": [{"name": "search", "arguments": {"q": "cats"}}]}'
    msg, finish = _parse_response(text, has_tools=True)
    assert msg.content is None
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].function.name == "search"
    # Dict arguments get re-serialized to a JSON string (OpenAI wire format).
    assert msg.tool_calls[0].function.arguments == '{"q": "cats"}'
    assert finish == "tool_calls"


def test_parse_response_tool_calls_arguments_string_passthrough() -> None:
    """When 'arguments' is already a string, forward it raw (no double-encode)."""
    text = '{"tool_calls": [{"name": "ping", "arguments": "raw-string"}]}'
    msg, finish = _parse_response(text, has_tools=True)
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].function.arguments == "raw-string"
    assert finish == "tool_calls"


def test_parse_response_tool_calls_in_markdown_fence() -> None:
    """Models often wrap JSON in ```json fences — extract_json_str handles it."""
    text = '```json\n{"tool_calls": [{"name": "x", "arguments": {}}]}\n```'
    msg, finish = _parse_response(text, has_tools=True)
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].function.name == "x"
    assert finish == "tool_calls"


def test_parse_response_malformed_json_falls_back_to_text() -> None:
    """Garbled JSON → return the original text, finish=stop, no crash."""
    msg, finish = _parse_response("{not valid json", has_tools=True)
    assert msg.content == "{not valid json"
    assert msg.tool_calls is None
    assert finish == "stop"


def test_parse_response_dict_without_tool_calls_key_falls_back() -> None:
    """Valid JSON but no tool_calls key → ship as content, no tool_calls."""
    text = '{"answer": 42}'
    msg, finish = _parse_response(text, has_tools=True)
    assert msg.content == text
    assert msg.tool_calls is None
    assert finish == "stop"


def test_parse_response_tool_call_missing_name_falls_back() -> None:
    """A tool_call missing 'name' raises KeyError mid-comprehension → fallback."""
    text = '{"tool_calls": [{"arguments": {}}]}'
    msg, finish = _parse_response(text, has_tools=True)
    assert msg.content == text
    assert msg.tool_calls is None
    assert finish == "stop"


def test_parse_response_empty_text_skips_parse_path() -> None:
    """Whitespace-only text shouldn't even enter the parse path."""
    msg, finish = _parse_response("   ", has_tools=True)
    assert msg.content == "   "
    assert msg.tool_calls is None
    assert finish == "stop"


# --- try_parse_json_lenient -------------------------------------------------


def test_try_parse_json_lenient_plain_object() -> None:
    parsed, cleaned = try_parse_json_lenient('{"a": 1}')
    assert parsed == {"a": 1}
    assert cleaned == '{"a": 1}'


def test_try_parse_json_lenient_strips_markdown_fence() -> None:
    parsed, cleaned = try_parse_json_lenient('```json\n{"x": 2}\n```')
    assert parsed == {"x": 2}
    assert cleaned == '{"x": 2}'


def test_try_parse_json_lenient_invalid_returns_none() -> None:
    parsed, _ = try_parse_json_lenient("nope {oops}")
    assert parsed is None


def test_try_parse_json_lenient_accepts_top_level_array() -> None:
    """extract_json_str recognizes arrays; we accept them rather than reject."""
    parsed, _ = try_parse_json_lenient("[1, 2, 3]")
    assert parsed == [1, 2, 3]


# --- _build_system_and_user -------------------------------------------------


def _req(**kwargs) -> ChatCompletionRequest:  # type: ignore[no-untyped-def]
    """Build a minimal request, override fields via kwargs."""
    return ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")], **kwargs)


def test_build_system_and_user_minimal_request() -> None:
    bundle = _build_system_and_user(_req())
    assert bundle.user == "hi"
    assert bundle.system == ""
    assert bundle.json_mode is False
    assert bundle.has_tools is False
    assert bundle.tool_choice == "auto"


def test_build_system_and_user_with_system_message() -> None:
    req = ChatCompletionRequest(
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="hello"),
        ],
    )
    bundle = _build_system_and_user(req)
    assert bundle.system == "be terse"
    assert bundle.user == "hello"


def test_build_system_and_user_appends_tool_prompt() -> None:
    tools = [ToolDefinition(function=ToolFunction(name="search", description="find things"))]
    bundle = _build_system_and_user(_req(tools=tools))
    assert bundle.has_tools is True
    assert "search" in bundle.system
    assert "tool_calls" in bundle.system  # the engineered format header


def test_build_system_and_user_tool_choice_none_skips_tools() -> None:
    """tool_choice='none' = act as if no tools were supplied."""
    tools = [ToolDefinition(function=ToolFunction(name="search"))]
    bundle = _build_system_and_user(_req(tools=tools, tool_choice="none"))
    assert bundle.has_tools is False
    assert "search" not in bundle.system


def test_build_system_and_user_json_mode_appends_instruction() -> None:
    bundle = _build_system_and_user(_req(response_format={"type": "json_object"}))
    assert bundle.json_mode is True
    assert "valid JSON only" in bundle.system


def test_build_system_and_user_rejects_unsupported_tool_choice() -> None:
    """Anything outside auto/none/required → HTTP 400, not silent acceptance."""
    from fastapi import HTTPException

    try:
        _build_system_and_user(_req(tool_choice="garbage"))
    except HTTPException as e:
        assert e.status_code == 400
        assert "garbage" in e.detail
    else:
        raise AssertionError("expected HTTPException")


def test_build_system_and_user_rejects_stream_true() -> None:
    """stream=true → HTTP 400 (see #179).

    Silently dropping ``stream`` served a single JSON body to callers
    expecting SSE ``chat.completion.chunk`` frames — the client hangs waiting
    for ``data:`` lines that never arrive, or crashes parsing one JSON blob
    as SSE. Fail loud until real SSE lands.
    """
    from fastapi import HTTPException

    try:
        _build_system_and_user(_req(stream=True))
    except HTTPException as e:
        assert e.status_code == 400
        assert "stream" in e.detail
    else:
        raise AssertionError("expected HTTPException")


def test_build_system_and_user_accepts_stream_false() -> None:
    """stream=false is the normal path — must not 400."""
    bundle = _build_system_and_user(_req(stream=False))
    assert bundle.user == "hi"


def test_build_system_and_user_accepts_stream_unset() -> None:
    """Omitting stream (the common case) must not 400."""
    bundle = _build_system_and_user(_req())
    assert bundle.user == "hi"


def test_chat_completion_request_declares_common_openai_fields() -> None:
    """Regression guard for #179: fields must be declared, not silently dropped.

    Prior to the fix, ChatCompletionRequest was a vanilla Pydantic model
    without ``extra="forbid"`` and without these fields declared, so
    Pydantic silently discarded them before ``_log_ignored_fields`` ran.
    Declaring them is what makes the ignored-field warning fire — this
    test pins the declaration so a future refactor can't quietly regress it.
    """
    declared = ChatCompletionRequest.model_fields
    for name in ("stream", "n", "stop", "presence_penalty", "frequency_penalty", "seed", "top_p", "logprobs"):
        assert name in declared, f"{name!r} must be declared on ChatCompletionRequest (see #179)"
