"""OpenAI Responses API adapter. The Harness acts on complete responses only; a call is
streamed when someone wants to read the agent's words as they come
(``ModelRequest.on_text``, D67), and the completed response at the end of the stream is
what it acts on."""

import contextlib
import json
import re
from typing import Any

import httpx

from witx.models.base import ModelError, ModelRequest, ModelResponse, model_listed

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
# Why a response may stop short and still be asked again. ``max_messages``: the model
# kept writing without calling a tool until the provider's cap on output items (101);
# a new sample usually calls one. Not ``max_output_tokens`` or ``content_filter``: the
# same request stops the same way.
RETRYABLE_INCOMPLETE = {"max_messages"}


class OpenAIResponses:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
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
        tools = request.tools + ([{"type": "web_search"}] if request.web_search else [])
        return {
            "model": request.model,
            "instructions": request.instructions,
            "input": request.input,
            "tools": tools,
            # The model chooses whether to call a tool: the instructions say when it
            # must, and words without one are a turn that ends. Forcing a call made
            # a model that meant to stop spin in its reasoning, and Claude's current
            # models refuse it outright.
            "parallel_tool_calls": False,
            # Explicit, so a change of the provider's default cannot lower it.
            "reasoning": {"effort": request.reasoning_effort or self._reasoning_effort},
            # Nothing is stored at the provider; WitX keeps the complete history itself.
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }

    async def ready(self, model: str) -> str:
        return await model_listed(self._client, model)

    async def respond(self, request: ModelRequest) -> ModelResponse:
        try:
            if request.on_text is not None:
                raw = await self._stream(request)
            else:
                response = await self._client.post("/responses", json=self.body(request))
                self._check(response)
                raw = response.json()
        except httpx.TimeoutException as error:
            raise ModelError("timeout", str(error), retryable=True) from None
        except httpx.TransportError as error:
            raise ModelError("network", str(error), retryable=True) from None
        return self._parsed(raw)

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code != 200:
            message = response.text[:2000]
            with contextlib.suppress(ValueError, KeyError, TypeError):
                message = response.json()["error"]["message"]
            raise ModelError(
                f"http_{response.status_code}",
                message,
                retryable=response.status_code in RETRYABLE_STATUS,
            )

    async def _stream(self, request: ModelRequest) -> dict[str, Any]:
        """The response as a stream of events: the agent's words are passed on as they
        grow, and the completed response at the end is returned whole."""
        assert request.on_text is not None
        words = Words()
        async with self._client.stream(
            "POST", "/responses", json=self.body(request) | {"stream": True}
        ) as response:
            if response.status_code != 200:
                await response.aread()
                self._check(response)
            event, data = "", ""
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                elif line == "":
                    done = await self._on_event(request, event, data, words) if data else None
                    event, data = "", ""
                    if done is not None:
                        return done
        raise ModelError("incomplete", "the stream ended before the response", retryable=True)

    async def _on_event(
        self, request: ModelRequest, event: str, data: str, words: "Words"
    ) -> dict[str, Any] | None:
        """Handles one event of the stream; the whole response once the stream has it."""
        try:
            payload = json.loads(data)
        except ValueError:
            return None
        phase = words.take(event, payload)
        if phase is not None:
            assert request.on_text is not None
            await request.on_text(words.text(), phase)
        elif event in ("response.completed", "response.incomplete", "response.failed"):
            return dict(payload.get("response") or {})
        elif event == "error":
            raise ModelError(
                str(payload.get("code") or "error"), str(payload.get("message", "")), retryable=True
            )
        return None

    @staticmethod
    def _parsed(raw: dict[str, Any]) -> ModelResponse:
        if raw.get("status") not in (None, "completed"):
            # An incomplete response (e.g. max tokens) may hold truncated tool arguments.
            reason = (raw.get("incomplete_details") or {}).get("reason", raw.get("status"))
            raise ModelError(
                "incomplete", str(reason), retryable=reason in RETRYABLE_INCOMPLETE, raw=raw
            )
        return ModelResponse(
            id=str(raw.get("id", "")),
            output=list(raw.get("output", [])),
            usage=raw.get("usage"),
            raw=raw,
        )


# The tool calls whose one argument is what the agent says to the user, and which one.
SPOKEN_ARGUMENTS = {"complete_task": "summary", "ask_user": "text"}


class Words:
    """The agent's words so far, out of a response stream: the text of its
    message, followed by the summary or question of a complete_task or ask_user call
    as its arguments stream in (a partial JSON string, read as far as it goes)."""

    def __init__(self) -> None:
        self.message = ""
        self.calls: dict[int, tuple[str, str]] = {}  # output index: tool name, arguments

    def take(self, event: str, payload: dict[str, Any]) -> str | None:
        """Takes in one stream event; what the model is at when the words changed
        ("talking": its message; "planning": a call's arguments), else None."""
        if event == "response.output_text.delta":
            self.message += str(payload.get("delta", ""))
            return "talking"
        index = int(payload.get("output_index", -1))
        if event == "response.output_item.added":
            item = payload.get("item") or {}
            if item.get("type") == "function_call":
                if item.get("name") in SPOKEN_ARGUMENTS:
                    self.calls[index] = (str(item["name"]), "")
                return "planning"
            return None
        if event == "response.function_call_arguments.delta" and index in self.calls:
            name, arguments = self.calls[index]
            self.calls[index] = (name, arguments + str(payload.get("delta", "")))
            return "planning"
        return None

    def text(self) -> str:
        spoken = [self.message.strip()]
        for name, arguments in self.calls.values():
            spoken.append(partial_string(arguments, SPOKEN_ARGUMENTS[name]))
        return "\n\n".join(s for s in spoken if s)


def partial_string(arguments: str, key: str) -> str:
    """The value of ``key`` in a JSON object that is still arriving: as far as the
    string goes, with its escapes resolved and an escape cut off at the end dropped."""
    found = re.search(rf'"{re.escape(key)}"\s*:\s*"', arguments)
    if found is None:
        return ""
    raw = re.match(r'(?:[^"\\]|\\.)*', arguments[found.end() :])
    fragment = raw.group(0) if raw else ""
    fragment = re.sub(r"\\u[0-9a-fA-F]{0,3}$", "", fragment).rstrip("\\")
    try:
        return str(json.loads(f'"{fragment}"'))
    except ValueError:
        return fragment


class OpenAIDecisions:
    """OpenAI's Decisions API (public beta, gpt-6-luna only): fixed questions about text
    and images, answered with probabilities. Used by the fast risk check."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def decide(
        self, *, model: str, input: list[dict[str, Any]], questions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The raw response; its ``answers`` hold one entry per question, by name."""
        try:
            response = await self._client.post(
                "/decisions", json={"model": model, "input": input, "questions": questions}
            )
        except httpx.TimeoutException as error:
            raise ModelError("timeout", str(error), retryable=True) from None
        except httpx.TransportError as error:
            raise ModelError("network", str(error), retryable=True) from None
        if response.status_code != 200:
            raise ModelError(
                f"http_{response.status_code}",
                response.text[:500],
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        raw: dict[str, Any] = response.json()
        return raw
