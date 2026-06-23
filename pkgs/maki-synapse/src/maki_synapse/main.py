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

Accepted-but-ignored (claude_agent_sdk.invoke_claude does not expose them):
  - temperature
  - max_tokens
  When a caller sets these explicitly, synapse logs a warning so the
  mismatch is visible rather than silent.

Response echoes the actual Claude model that served the request, not the
`model` field from the request — `invoke_claude` always uses the `MODEL`
env var, and lying about it would mislead clients that log/assert on it.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from maki_common import configure_logging
from maki_common.claude import TokenUsage, invoke_claude
from pydantic import BaseModel

configure_logging()
log = logging.getLogger(__name__)

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_QUERIES", "3"))
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

SUPPORTED_TOOL_CHOICE = ("auto", "none", "required")

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


class ChatCompletionRequest(BaseModel):
    model: str = MODEL
    messages: list[ChatMessage]
    tools: list[ToolDefinition] | None = None
    tool_choice: str | None = "auto"
    temperature: float | None = 0
    max_tokens: int | None = 2000
    response_format: dict | None = None


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
            user_parts.append(msg.content or "")
        elif msg.role == "assistant":
            parts: list[str] = []
            if msg.content:
                parts.append(msg.content)
            if msg.tool_calls:
                call_descs = [f"{tc.function.name}({tc.function.arguments})" for tc in msg.tool_calls]
                parts.append("[Tool calls: " + "; ".join(call_descs) + "]")
            user_parts.append(f"Assistant: {' '.join(parts).strip()}")
        elif msg.role in ("tool", "function"):
            ident = msg.name or msg.tool_call_id or "tool"
            user_parts.append(f"[Tool result from {ident}]: {msg.content or ''}")
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


def _log_ignored_fields(req: ChatCompletionRequest) -> None:
    """Warn when callers set fields that invoke_claude cannot honor."""
    ignored: dict[str, Any] = {}
    fields_set = req.model_fields_set
    if "temperature" in fields_set:
        ignored["temperature"] = req.temperature
    if "max_tokens" in fields_set:
        ignored["max_tokens"] = req.max_tokens
    if ignored:
        log.warning(
            "synapse does not forward these OpenAI fields to the Claude SDK; they are ignored",
            extra={"ignored_fields": ignored},
        )


# --- Endpoints ---


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    return {"data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    # Validate tool_choice up front — silent mis-handling is the whole point
    # of this endpoint's prior bug.
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

    json_mode = req.response_format and req.response_format.get("type") == "json_object"
    if json_mode:
        system_prompt += (
            "\n\nIMPORTANT: You MUST respond with valid JSON only. "
            "No explanation, no markdown fencing, no text before or after the JSON. "
            "Output a single JSON object starting with { and ending with }."
        )

    user_prompt = "\n".join(user_parts)
    request_id = f"synapse-{uuid.uuid4().hex[:12]}"

    try:
        log.info(
            "Invoking Claude",
            extra={
                "tools": bool(effective_tools),
                "tool_choice": tool_choice,
                "user_prompt_len": len(user_prompt),
            },
        )
        text, token_usage = await invoke_claude(
            user_prompt,
            model=MODEL,
            semaphore=_semaphore,
            system_prompt=system_prompt or None,
            mode="synapse_proxy",
        )
        log.info("Claude response", extra={"response_len": len(text)})

        # JSON mode: validate extraction, retry once on failure.
        # On success, replace text with the extracted JSON so callers receive a
        # clean json.loads-able payload (not the original markdown-fenced text).
        if json_mode:
            raw = extract_json_str(text)
            try:
                json.loads(raw)
                text = raw
            except json.JSONDecodeError:
                log.warning("JSON extraction failed, retrying", extra={"raw_preview": text[:200]})
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
                # Both calls were billed — accumulate so the OpenAI Usage we
                # return reflects the real cost, not just the retry's.
                token_usage = _add_token_usages(token_usage, retry_usage)
                text = retry_text
                log.info("Retry response", extra={"response_len": len(text)})
                retry_raw = extract_json_str(text)
                try:
                    json.loads(retry_raw)
                    text = retry_raw
                except json.JSONDecodeError:
                    # Log the accumulated cost so the wasted attempt is
                    # visible in telemetry even though the 502 response body
                    # carries no usage field.
                    log.error(
                        "JSON retry also failed, returning 502",
                        extra={
                            "raw_preview": text[:200],
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
        log.warning("Claude invocation timed out")
        raise HTTPException(status_code=502, detail="upstream timeout")
    except Exception as e:
        log.exception("Claude invocation failed")
        raise HTTPException(status_code=502, detail=str(e))

    usage = _map_usage(token_usage)

    # Parse response
    message: ResponseMessage
    finish_reason = "stop"

    if effective_tools and text.strip():
        try:
            raw = extract_json_str(text)
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "tool_calls" in parsed:
                tool_calls = [
                    ToolCallItem(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        function=ToolCallFunction(
                            name=tc["name"],
                            arguments=(
                                json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"]
                            ),
                        ),
                    )
                    for tc in parsed["tool_calls"]
                ]
                message = ResponseMessage(content=None, tool_calls=tool_calls)
                finish_reason = "tool_calls"
            else:
                message = ResponseMessage(content=text)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("Failed to parse tool response, returning as text", extra={"error": str(e)})
            message = ResponseMessage(content=text)
    else:
        message = ResponseMessage(content=text)

    return ChatCompletionResponse(
        id=f"chatcmpl-{request_id}",
        created=int(time.time()),
        # Echo the actual model that served the request, not req.model.
        # invoke_claude always uses MODEL, so reporting anything else would
        # mislead clients that log or assert on response.model.
        model=MODEL,
        choices=[Choice(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def cli():
    import uvicorn

    uvicorn.run("maki_synapse.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    cli()
