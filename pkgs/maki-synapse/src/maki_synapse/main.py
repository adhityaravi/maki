"""maki-synapse: OpenAI-compatible LLM proxy backed by Claude Agent SDK.

Translates OpenAI chat completion requests (including tool calling) into
Claude SDK query() calls, using the host's Claude subscription via OAuth.

OpenAI-compat support matrix
----------------------------
Honored:
  - messages (roles: system, user, assistant, tool)
  - tools (prompt-engineered into the system prompt)
  - tool_choice: "auto" | "none" | "required"  (anything else → HTTP 400)
  - response_format={"type":"json_object"}

Rejected (HTTP 400) — wire format incompatible with the current handler:
  - stream=true  — synapse serves a single ``chat.completion`` body, not SSE
    ``chat.completion.chunk`` frames. Silently downgrading would leave
    default-streaming OpenAI clients (``AsyncOpenAI(...).chat.completions
    .create(stream=True, ...)``, most LangChain/LlamaIndex paths) hanging on
    ``data:`` frames that never arrive, or crashing while parsing a single
    JSON body as SSE. See #179.

Rejected (HTTP 422) — undeclared OpenAI fields:
  ``ChatCompletionRequest`` uses ``extra="forbid"``, so any Chat Completions
  field we haven't explicitly declared (``logit_bias``, ``top_logprobs``,
  ``user``, ``parallel_tool_calls``, and anything OpenAI adds later) 422s
  with ``"Extra inputs are not permitted"`` instead of being silently
  dropped by Pydantic. Advertising OpenAI-compat while discarding half the
  schema is a footgun for future callers and hides bugs like #179. See #180.

Accepted-but-ignored (claude_agent_sdk.invoke_claude does not expose them):
  - temperature
  - max_tokens
  - n, stop, presence_penalty, frequency_penalty, seed, top_p, logprobs
  When a caller sets any of these explicitly, synapse logs a warning so the
  mismatch is visible rather than silent (see #179). These are declared on
  the request model — with ``extra="forbid"`` in place they'd otherwise
  422 — so ``_log_ignored_fields`` sees them and warns instead.

Response echoes the actual Claude model that served the request, not the
`model` field from the request — `invoke_claude` always uses the `MODEL`
env var, and lying about it would mislead clients that log/assert on it.

Internal shape
--------------
``chat_completions`` is intentionally thin — it delegates to three single-
responsibility helpers so each piece can be unit-tested without spinning up
FastAPI or mocking NATS (see #112):

  - ``_build_system_and_user(req) -> PromptBundle`` — validate + assemble.
  - ``_invoke_with_json_retry(...)`` — call Claude with one corrective retry
    on JSON-mode parse failure, accumulating token usage across both calls.
  - ``_parse_response(text, has_tools=...)`` — decode tool_calls JSON,
    falling back to plain text on shape mismatch.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from maki_common import DEFAULT_CLAUDE_MODEL, configure_logging
from maki_common.claude import TokenUsage, invoke_claude
from pydantic import BaseModel, ConfigDict

configure_logging()
log = logging.getLogger(__name__)

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_QUERIES", "3"))
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

MODEL = os.environ.get("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)

SUPPORTED_TOOL_CHOICE = ("auto", "none", "required")

# Appended to the system prompt when response_format={"type":"json_object"}.
# Kept as a module constant so the wording is reviewable in one place — the
# model is sensitive to "no markdown fencing" being literal.
JSON_MODE_INSTRUCTION = (
    "\n\nIMPORTANT: You MUST respond with valid JSON only. "
    "No explanation, no markdown fencing, no text before or after the JSON. "
    "Output a single JSON object starting with { and ending with }."
)

app = FastAPI(title="maki-synapse", version="0.0.1")


# --- Request / Response models (OpenAI-compatible subset) ---


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCallItem(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    # Present on role="tool" messages: identifies which assistant tool_call
    # this message is the result of.
    tool_call_id: str | None = None
    # Optional function/tool name (used with role="tool" and legacy
    # role="function" messages).
    name: str | None = None
    # Present on role="assistant" messages that previously invoked tools
    # in a multi-turn flow.
    tool_calls: list[ToolCallItem] | None = None


class ToolFunction(BaseModel):
    name: str
    description: str = ""
    parameters: dict = {}


class ToolDefinition(BaseModel):
    type: str = "function"
    function: ToolFunction


# ``extra="forbid"`` makes any OpenAI Chat Completions field we haven't
# explicitly declared 422 with ``"Extra inputs are not permitted"`` instead
# of being silently dropped by Pydantic's default ``extra="ignore"``. Synapse
# advertises itself as OpenAI-compatible; silently discarding half the schema
# (``logit_bias``, ``top_logprobs``, ``user``, ``parallel_tool_calls``, …)
# turns any future caller's bug into a mystery instead of a clean error.
# See #180. Fields we *do* accept-but-ignore stay declared below so callers
# get a warning via ``_log_ignored_fields`` rather than a 422.
class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = MODEL
    messages: list[ChatMessage]
    tools: list[ToolDefinition] | None = None
    tool_choice: str | None = "auto"
    temperature: float | None = 0
    max_tokens: int | None = 2000
    response_format: dict | None = None
    # Declared so the very common ``{"stream": true, ...}`` body gets a clear
    # 400 explaining SSE isn't wired yet, instead of the generic 422 that
    # ``extra="forbid"`` would produce for an undeclared field. Prior to
    # declaring it, Pydantic silently dropped ``stream`` and served a single
    # non-streaming ``chat.completion`` body, leaving SSE-parsing clients
    # hanging on ``data:`` frames that never arrive (see #179).
    stream: bool | None = None
    # Accepted-but-ignored OpenAI fields — declared so ``_log_ignored_fields``
    # can warn when a caller sets them, instead of the 422 that
    # ``extra="forbid"`` would otherwise produce. invoke_claude does not
    # expose any of these to the Claude SDK.
    n: int | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    top_p: float | None = None
    logprobs: bool | None = None


class ResponseMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCallItem] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Usage()


# --- Tool prompt building ---


def build_tool_prompt(tools: list[ToolDefinition], *, required: bool = False) -> str:
    tool_descs = []
    for t in tools:
        tool_descs.append(
            f"- {t.function.name}: {t.function.description}\n  Parameters schema: {json.dumps(t.function.parameters)}"
        )
    header = (
        "\n\n---\n"
        "You have access to the following tools. To call a tool, respond ONLY "
        "with a JSON object in this exact format (no markdown, no explanation, no extra text):\n"
        '{"tool_calls": [{"name": "<tool_name>", "arguments": {<arguments>}}]}\n\n'
    )
    if required:
        header += "You MUST call one of the tools below. Do not answer in plain text.\n\n"
    else:
        header += "If you don't need to call any tools, respond with plain text.\n\n"
    return header + "Available tools:\n" + "\n".join(tool_descs)


# --- Message serialization for prompt-engineered tool flow ---


def _serialize_messages(messages: list[ChatMessage]) -> tuple[list[str], list[str]]:
    """Flatten OpenAI-style messages into (system_parts, user_parts).

    Each non-system message is wrapped in ``<turn role="...">...</turn>`` so
    role boundaries survive the collapse into a single Claude user prompt.
    Before this encoding, user turns were emitted as bare content and only
    ``assistant`` / ``tool`` got a role marker; a multi-turn
    ``user → assistant → user`` sequence lost the second user boundary and
    Claude conflated the turns (see #184). Wrapping every turn keeps the
    encoding symmetric and harder to confuse with prose that happens to
    contain the substring ``User:`` or ``Assistant:``. The tag shape matches
    the ``<turn role="…">…</turn>`` convention already used by
    ``maki_cortex.main._format_conversation_history``.

    The Claude Agent SDK has no notion of multi-turn tool results coming back
    from the caller, so we inline prior assistant tool_calls and tool results
    into the prompt stream as tagged text. This is best-effort: Mem0 (our
    only caller today) never sends these, but any future multi-turn tool
    caller should at least round-trip without data loss.
    """
    system_parts: list[str] = []
    user_parts: list[str] = []
    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.content or "")
        elif msg.role == "user":
            user_parts.append(f'<turn role="user">{msg.content or ""}</turn>')
        elif msg.role == "assistant":
            parts: list[str] = []
            if msg.content:
                parts.append(msg.content)
            if msg.tool_calls:
                call_descs = [f"{tc.function.name}({tc.function.arguments})" for tc in msg.tool_calls]
                parts.append("[Tool calls: " + "; ".join(call_descs) + "]")
            user_parts.append(f'<turn role="assistant">{" ".join(parts).strip()}</turn>')
        elif msg.role in ("tool", "function"):
            ident = msg.name or msg.tool_call_id or "tool"
            user_parts.append(f'<turn role="tool" name="{ident}">{msg.content or ""}</turn>')
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported message role: {msg.role!r}",
            )
    return system_parts, user_parts


# --- JSON extraction ---


def extract_json_str(text: str) -> str:
    """Extract JSON from text that may contain markdown code blocks or preamble."""
    text = text.strip()
    # Markdown code block
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Direct JSON object or array
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    # JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def try_parse_json_lenient(text: str) -> tuple[Any, str]:
    """Extract and parse JSON from text with markdown/preamble tolerance.

    Returns ``(parsed, cleaned)``:
      - ``parsed`` is the decoded value, or ``None`` when extraction produced
        invalid JSON.
      - ``cleaned`` is always ``extract_json_str(text)`` so callers in JSON
        mode can swap a markdown-fenced response for the bare JSON payload
        without re-running extraction.

    ``parsed`` is typed ``Any`` rather than ``dict | None`` because
    ``extract_json_str`` also recognizes top-level arrays — we accept them
    rather than re-classify them as not-JSON.

    Consolidates the ``extract_json_str(text) + json.loads(raw)`` pattern
    that previously lived inline at all three call sites in chat_completions
    (see #112).
    """
    raw = extract_json_str(text)
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return None, raw


# --- Token usage helpers ---


def _map_usage(token_usage: TokenUsage) -> Usage:
    """Map Claude TokenUsage to the OpenAI Usage schema.

    prompt_tokens includes cache_read and cache_creation tokens so that callers
    see the real billable input-token count rather than zeros.
    """
    return Usage(
        prompt_tokens=token_usage.input_tokens + token_usage.cache_read_tokens + token_usage.cache_creation_tokens,
        completion_tokens=token_usage.output_tokens,
        total_tokens=token_usage.total_tokens,
    )


def _add_token_usages(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    """Sum two TokenUsage records across billable + additive fields.

    Used when a single request triggers multiple Claude calls (e.g. JSON-mode
    retry): both calls are billed, so both must be reflected in the usage we
    return to OpenAI-compatible clients. Metadata fields (model, mode) are
    kept from the first call since that defines the request's identity.
    """
    if a.total_cost_usd is None and b.total_cost_usd is None:
        total_cost: float | None = None
    else:
        total_cost = (a.total_cost_usd or 0.0) + (b.total_cost_usd or 0.0)
    return TokenUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
        total_cost_usd=total_cost,
        num_turns=a.num_turns + b.num_turns,
        model=a.model,
        mode=a.mode,
        duration_ms=a.duration_ms + b.duration_ms,
        model_usage={**a.model_usage, **b.model_usage},
    )


# Fields declared on ChatCompletionRequest that invoke_claude does not
# forward to the Claude SDK. Kept as a module-level tuple so adding a new
# accepted-but-ignored field is a one-line change and the module docstring's
# support matrix can be diff-audited against it.
_ACCEPTED_BUT_IGNORED_FIELDS: tuple[str, ...] = (
    "temperature",
    "max_tokens",
    "n",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "top_p",
    "logprobs",
)


def _log_ignored_fields(req: ChatCompletionRequest) -> None:
    """Warn when callers set fields that invoke_claude cannot honor.

    Only fires for fields the caller *explicitly set* (via
    ``model_fields_set``) — the model's own defaults for ``temperature`` /
    ``max_tokens`` would otherwise trip a warning on every request.
    """
    fields_set = req.model_fields_set
    ignored: dict[str, Any] = {name: getattr(req, name) for name in _ACCEPTED_BUT_IGNORED_FIELDS if name in fields_set}
    if ignored:
        log.warning(
            "synapse does not forward these OpenAI fields to the Claude SDK; they are ignored",
            extra={"ignored_fields": ignored},
        )


# --- Request prep / invocation / response parsing ---


@dataclass(frozen=True)
class PromptBundle:
    """Decoded request: assembled prompts plus the flags downstream needs.

    ``has_tools`` reflects the *effective* tool set after applying
    ``tool_choice="none"`` filtering, so the parser can use it directly to
    decide whether to attempt tool_calls extraction.
    """

    system: str
    user: str
    json_mode: bool
    has_tools: bool
    tool_choice: str


def _build_system_and_user(req: ChatCompletionRequest) -> PromptBundle:
    """Validate the request and assemble the system + user prompts.

    Owns: tool_choice validation, ignored-field warnings, message flattening,
    tool-prompt injection, JSON-mode instruction. Returns a frozen bundle so
    the orchestrator can pass strings + flags around without re-deriving them.
    """
    # Reject stream=true up front — silently dropping it (Pydantic's old
    # behavior, before we declared the field) served a single non-streaming
    # ``chat.completion`` body to callers expecting SSE ``chat.completion
    # .chunk`` frames, which either hangs the client or crashes the parser.
    # Better to fail loud with a clear 400 until real SSE lands. See #179.
    if req.stream:
        raise HTTPException(
            status_code=400,
            detail="synapse does not yet support stream=true; pass stream=false",
        )

    tool_choice = req.tool_choice or "auto"
    if tool_choice not in SUPPORTED_TOOL_CHOICE:
        raise HTTPException(
            status_code=400,
            detail=(f"Unsupported tool_choice: {req.tool_choice!r}. Supported values: {list(SUPPORTED_TOOL_CHOICE)}."),
        )

    _log_ignored_fields(req)

    system_parts, user_parts = _serialize_messages(req.messages)
    system_prompt = "\n".join(system_parts)

    # tool_choice="none" means: act as if no tools were supplied.
    effective_tools = req.tools if tool_choice != "none" else None
    if effective_tools:
        system_prompt += build_tool_prompt(effective_tools, required=(tool_choice == "required"))

    json_mode = bool(req.response_format and req.response_format.get("type") == "json_object")
    if json_mode:
        system_prompt += JSON_MODE_INSTRUCTION

    return PromptBundle(
        system=system_prompt,
        user="\n".join(user_parts),
        json_mode=json_mode,
        has_tools=bool(effective_tools),
        tool_choice=tool_choice,
    )


async def _invoke_with_json_retry(
    user_prompt: str,
    system_prompt: str,
    *,
    json_mode: bool,
    request_id: str,
) -> tuple[str, TokenUsage]:
    """Invoke Claude, retrying once with a corrective suffix on JSON parse failure.

    Returns ``(text, token_usage)``:
      - In JSON mode, ``text`` is the *cleaned* extracted JSON (markdown
        fences and preamble stripped) so the caller can serve it as
        ``content`` without re-running extraction.
      - ``token_usage`` is cumulative across both calls when a retry fires —
        both invocations were billed, so both must be reflected in the
        OpenAI Usage we surface (fixes #107, which dropped first-call tokens).

    Wraps both invocations in a single try/except so upstream failures map
    consistently to HTTP 502 with opaque bodies — ``"upstream timeout"`` for
    TimeoutError, ``"upstream error"`` for everything else. Raw exception
    text is kept out of the response because the Claude SDK / httpx / anyio
    layers surface Postgres DSN fragments, provider paths, and traceback
    hints in ``str(e)`` that leak straight to the caller (see #350 for the
    timeout half of this reasoning, #158 for the generic half). Full
    exception including traceback is preserved server-side via
    ``log.exception``. A retry that still fails to parse JSON also raises
    502 with the accumulated usage logged for cost triage.
    """
    try:
        text, token_usage = await invoke_claude(
            user_prompt,
            model=MODEL,
            semaphore=_semaphore,
            system_prompt=system_prompt or None,
            mode="synapse_proxy",
        )
        log.info("Claude response", extra={"request_id": request_id, "response_len": len(text)})

        if not json_mode:
            return text, token_usage

        # JSON mode: validate extraction. On success the cleaned string replaces
        # text so callers receive a clean json.loads-able payload, not the
        # original markdown-fenced version.
        parsed, cleaned = try_parse_json_lenient(text)
        if parsed is not None:
            return cleaned, token_usage

        log.warning(
            "JSON extraction failed, retrying",
            extra={"request_id": request_id, "raw_preview": text[:200]},
        )
        retry_prompt = (
            f"{user_prompt}\n\n"
            "Your previous response was not valid JSON. "
            "Respond with ONLY the raw JSON object. No other text."
        )
        retry_text, retry_usage = await invoke_claude(
            retry_prompt,
            model=MODEL,
            semaphore=_semaphore,
            system_prompt=system_prompt or None,
            mode="synapse_proxy_retry",
        )
        # Both calls were billed — accumulate so the OpenAI Usage we return
        # reflects the real cost, not just the retry's.
        token_usage = _add_token_usages(token_usage, retry_usage)
        log.info("Retry response", extra={"request_id": request_id, "response_len": len(retry_text)})

        retry_parsed, retry_cleaned = try_parse_json_lenient(retry_text)
        if retry_parsed is not None:
            return retry_cleaned, token_usage

        # Log the accumulated cost so the wasted attempt is visible in
        # telemetry even though the 502 response body carries no usage field.
        log.error(
            "JSON retry also failed, returning 502",
            extra={
                "request_id": request_id,
                "raw_preview": retry_text[:200],
                "token_usage": token_usage.to_log_dict(),
            },
        )
        raise HTTPException(status_code=502, detail="Model failed to return valid JSON after retry")
    except HTTPException:
        raise
    except TimeoutError:
        # The wrapper enforces a hard per-call deadline (DEFAULT_INVOKE_TIMEOUT_S);
        # surfacing str(e) here would leak elapsed-seconds and prompt metadata
        # into a public-ish error body. Keep the body opaque ("upstream timeout")
        # and rely on the structured log inside invoke_claude for triage
        # (mode, prompt_len, elapsed_ms). Also pairs with #158. See #350.
        log.warning("Claude invocation timed out", extra={"request_id": request_id})
        raise HTTPException(status_code=502, detail="upstream timeout")
    except Exception:
        # ``detail=str(e)`` used to be here; it leaked exception text from
        # the Claude SDK / httpx / anyio layers straight into the response
        # body (DSN fragments, provider file paths, traceback-adjacent
        # detail). Full exception is preserved in the server log via
        # ``log.exception``; the response body stays opaque. See #158.
        log.exception("Claude invocation failed", extra={"request_id": request_id})
        raise HTTPException(status_code=502, detail="upstream error")


def _parse_response(text: str, *, has_tools: bool) -> tuple[ResponseMessage, str]:
    """Decode Claude's text output into ``(ResponseMessage, finish_reason)``.

    With tools enabled and non-empty text, attempts to extract a
    ``{"tool_calls": [...]}`` payload. Falls back to plain text whenever the
    output is malformed JSON, the JSON shape doesn't match, or individual
    tool_call entries are missing required fields.
    """
    if not (has_tools and text.strip()):
        return ResponseMessage(content=text), "stop"

    parsed, _ = try_parse_json_lenient(text)
    if parsed is None:
        # Preserve the original log: when tools are enabled and Claude
        # returns something that doesn't parse, callers want a breadcrumb.
        log.warning("Failed to parse tool response, returning as text")
        return ResponseMessage(content=text), "stop"
    if not isinstance(parsed, dict) or "tool_calls" not in parsed:
        return ResponseMessage(content=text), "stop"

    try:
        tool_calls = [
            ToolCallItem(
                id=f"call_{uuid.uuid4().hex[:8]}",
                function=ToolCallFunction(
                    name=tc["name"],
                    arguments=(json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"]),
                ),
            )
            for tc in parsed["tool_calls"]
        ]
    except (KeyError, TypeError) as e:
        log.warning("Failed to parse tool response, returning as text", extra={"error": str(e)})
        return ResponseMessage(content=text), "stop"

    return ResponseMessage(content=None, tool_calls=tool_calls), "tool_calls"


# --- Endpoints ---


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    return {"data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-compatible chat completions.

    Thin orchestrator: build → invoke → parse. Each step lives in its own
    helper so this body fits on one screen and the helpers are unit-testable
    without spinning up FastAPI or mocking NATS (see #112).
    """
    request_id = f"synapse-{uuid.uuid4().hex[:12]}"
    prompt = _build_system_and_user(req)

    log.info(
        "Invoking Claude",
        extra={
            "request_id": request_id,
            "tool_choice": prompt.tool_choice,
            "has_tools": prompt.has_tools,
            "json_mode": prompt.json_mode,
            "user_prompt_len": len(prompt.user),
        },
    )
    text, token_usage = await _invoke_with_json_retry(
        prompt.user,
        prompt.system,
        json_mode=prompt.json_mode,
        request_id=request_id,
    )
    message, finish_reason = _parse_response(text, has_tools=prompt.has_tools)

    return ChatCompletionResponse(
        id=f"chatcmpl-{request_id}",
        created=int(time.time()),
        # Echo the actual model that served the request, not req.model.
        # invoke_claude always uses MODEL, so reporting anything else would
        # mislead clients that log or assert on response.model.
        model=MODEL,
        choices=[Choice(message=message, finish_reason=finish_reason)],
        usage=_map_usage(token_usage),
    )


def cli():
    import uvicorn

    uvicorn.run("maki_synapse.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    cli()
