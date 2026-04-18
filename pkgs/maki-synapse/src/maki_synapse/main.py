"""maki-synapse: OpenAI-compatible LLM proxy backed by Claude Agent SDK.

Translates OpenAI chat completion requests (including tool calling) into
Claude SDK query() calls, using the host's Claude subscription via OAuth.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid

from fastapi import FastAPI, HTTPException
from maki_common import configure_logging
from maki_common.claude import invoke_claude
from pydantic import BaseModel

configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="maki-synapse", version="0.0.1")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_QUERIES", "3"))
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")


# --- Request / Response models (OpenAI-compatible subset) ---


class ChatMessage(BaseModel):
    role: str
    content: str | None = None


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


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCallItem(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


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


def build_tool_prompt(tools: list[ToolDefinition]) -> str:
    tool_descs = []
    for t in tools:
        tool_descs.append(
            f"- {t.function.name}: {t.function.description}\n  Parameters schema: {json.dumps(t.function.parameters)}"
        )
    return (
        "\n\n---\n"
        "You have access to the following tools. To call a tool, respond ONLY "
        "with a JSON object in this exact format (no markdown, no explanation, no extra text):\n"
        '{"tool_calls": [{"name": "<tool_name>", "arguments": {<arguments>}}]}\n\n'
        "If you don't need to call any tools, respond with plain text.\n\n"
        "Available tools:\n" + "\n".join(tool_descs)
    )


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


# --- Endpoints ---


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    return {"data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    system_parts = []
    user_parts = []

    for msg in req.messages:
        if msg.role == "system":
            system_parts.append(msg.content or "")
        elif msg.role == "user":
            user_parts.append(msg.content or "")
        elif msg.role == "assistant":
            user_parts.append(f"Assistant: {msg.content or ''}")

    system_prompt = "\n".join(system_parts)

    if req.tools:
        system_prompt += build_tool_prompt(req.tools)

    json_mode = req.response_format and req.response_format.get("type") == "json_object"
    if json_mode:
        system_prompt += (
            "\n\nIMPORTANT: You MUST respond with valid JSON only. "
            "No explanation, no markdown fencing, no text before or after the JSON. "
            "Output a single JSON object starting with { and ending with }."
        )

    user_prompt = "\n".join(user_parts)

    try:
        log.info("Invoking Claude", extra={"tools": bool(req.tools), "user_prompt_len": len(user_prompt)})
        text, _usage = await invoke_claude(
            user_prompt,
            model=MODEL,
            semaphore=_semaphore,
            system_prompt=system_prompt or None,
        )
        log.info("Claude response", extra={"response_len": len(text)})

        # JSON mode: validate extraction, retry once on failure
        if json_mode:
            raw = extract_json_str(text)
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                log.warning("JSON extraction failed, retrying", extra={"raw_preview": text[:200]})
                retry_prompt = (
                    f"{user_prompt}\n\n"
                    "Your previous response was not valid JSON. "
                    "Respond with ONLY the raw JSON object. No other text."
                )
                text, _usage = await invoke_claude(
                    retry_prompt,
                    model=MODEL,
                    semaphore=_semaphore,
                    system_prompt=system_prompt or None,
                )
                log.info("Retry response", extra={"response_len": len(text)})
    except Exception as e:
        log.exception("Claude invocation failed")
        raise HTTPException(status_code=502, detail=str(e))

    # Parse response
    message: ResponseMessage
    finish_reason = "stop"

    if req.tools and text.strip():
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
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created=int(time.time()),
        model=req.model,
        choices=[Choice(message=message, finish_reason=finish_reason)],
    )


def cli():
    import uvicorn

    uvicorn.run("maki_synapse.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    cli()
