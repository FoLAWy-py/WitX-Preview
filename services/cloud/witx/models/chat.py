"""OpenAI-compatible Chat Completions adapter, for providers without the Responses API
(DeepInfra). It translates the Harness's Responses-shaped items into chat messages and
the reply back into items, so a task can switch providers between two steps."""

import contextlib
import json
from typing import Any

import httpx

from witx.models.base import ModelError, ModelRequest, ModelResponse, model_listed
from witx.models.openai import RETRYABLE_STATUS

# Chat Completions knows fewer levels; Claude's models through it take all of ours.
EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _effort(model: str, effort: str) -> str:
    return effort if model.startswith("anthropic/") else EFFORTS[effort]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "")
        for part in content or []
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text")
    )


def _user_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for part in content or []:
        kind = part.get("type")
        if kind in ("input_text", "text"):
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind == "input_image" and part.get("image_url"):
            parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
    return parts


def messages(instructions: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Responses items as chat messages. Consecutive tool calls (and the assistant text
    before them) form one assistant message; reasoning items stay with their provider."""
    out: list[dict[str, Any]] = [{"role": "system", "content": instructions}]
    assistant: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal assistant
        if assistant is not None:
            out.append(assistant)
            assistant = None

    for item in items:
        kind = item.get("type", "message")
        if kind == "message" and item.get("role") == "assistant":
            flush()
            assistant = {"role": "assistant", "content": _text(item.get("content"))}
        elif kind == "function_call":
            if assistant is None:
                assistant = {"role": "assistant", "content": None}
            assistant.setdefault("tool_calls", []).append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item.get("arguments", "{}")},
                }
            )
        elif kind == "function_call_output":
            flush()
            output = item.get("output", "")
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": output if isinstance(output, str) else json.dumps(output),
                }
            )
        elif kind == "message":
            flush()
            role = "user" if item.get("role") in (None, "user") else "system"
            content = _user_content(item.get("content"))
            out.append({"role": role, "content": content if role == "user" else _text(content)})
        # "reasoning" and anything else: not portable, left out
    flush()
    return out


def tools(responses_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for t in responses_tools
        if t.get("type", "function") == "function"
    ]


def output_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The assistant's reply as Responses items (text, then tool calls)."""
    items: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text.strip():
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        items.append(
            {
                "type": "function_call",
                "call_id": call.get("id") or "",
                "name": function.get("name", ""),
                "arguments": function.get("arguments") or "{}",
            }
        )
    return items


class ChatCompletions:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        reasoning_effort: str = "medium",
        timeout: float = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._reasoning_effort = reasoning_effort

    def body(self, request: ModelRequest) -> dict[str, Any]:
        # As with the Responses adapter, the model chooses whether to call a tool;
        # these fields need tools to be sent (a side chat has none).
        acting = (
            {"tools": tools(request.tools), "parallel_tool_calls": False} if request.tools else {}
        )
        return {
            "model": request.model,
            "messages": messages(request.instructions, request.input),
            **acting,
            "reasoning_effort": _effort(
                request.model, request.reasoning_effort or self._reasoning_effort
            ),
        }

    async def ready(self, model: str) -> str:
        return await model_listed(self._client, model)

    async def respond(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._client.post("/chat/completions", json=self.body(request))
        except httpx.TimeoutException as error:
            raise ModelError("timeout", str(error), retryable=True) from None
        except httpx.TransportError as error:
            raise ModelError("network", str(error), retryable=True) from None
        if response.status_code != 200:
            message = response.text[:2000]
            with contextlib.suppress(ValueError, KeyError, TypeError):
                detail = response.json().get("error")
                message = detail["message"] if isinstance(detail, dict) else str(detail)
            raise ModelError(
                f"http_{response.status_code}",
                message,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        raw = response.json()
        choices = raw.get("choices") or []
        if not choices:
            raise ModelError("empty", "no choices in the response", retryable=True)
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ModelError("incomplete", "max_output_tokens", retryable=False)
        usage = raw.get("usage") or {}
        return ModelResponse(
            id=str(raw.get("id", "")),
            output=output_items(choice.get("message") or {}),
            # The same keys as the Responses API, so usage sums across providers.
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            raw=raw,
        )
