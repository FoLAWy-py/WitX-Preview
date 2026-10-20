"""The interface the Harness uses to call a model.

Items follow the OpenAI Responses shape (``message``, ``function_call``,
``function_call_output``); other providers translate at their adapter.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class ModelRequest:
    model: str
    instructions: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    # The tenant's choice of providers its data may go to (None: the owner has not
    # chosen, so only the default model's provider). Required, so that no call skips
    # the check; the router refuses any other provider (rules.check_provider).
    allowed_providers: tuple[str, ...] | None
    # The provider's own web search, where it has one (OpenAI); others ignore it.
    web_search: bool = False
    # medium, high, xhigh or max; None: the adapter's default.
    reasoning_effort: str | None = None
    # Called with the agent's words so far as the model produces them: the text
    # of its message, and the summary or question of a complete_task or ask_user call
    # while its arguments stream, with what the model is at ("talking" for message
    # text, "planning" for a call's arguments). An adapter that can stream calls it
    # with the whole text each time it grows; one that cannot never calls it. None:
    # nobody is watching.
    on_text: Callable[[str, str], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class ModelResponse:
    id: str
    output: list[dict[str, Any]]
    usage: dict[str, Any] | None
    raw: dict[str, Any] = field(repr=False)


class ModelError(Exception):
    """A failed model call. ``retryable`` errors are retried later with backoff. ``raw``
    is what the provider answered, when it answered: kept on the failed step."""

    def __init__(
        self, code: str, message: str, *, retryable: bool, raw: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable
        self.raw = raw


class ModelClient(Protocol):
    async def respond(self, request: ModelRequest) -> ModelResponse: ...

    async def ready(self, model: str) -> str:
        """ "ok", or why ``model`` cannot be used now; no inference is run."""
        ...


async def model_listed(client: httpx.AsyncClient, model: str) -> str:
    """Whether an OpenAI-compatible API accepts the key and offers ``model``: its free
    model list, so a readiness check costs no tokens."""
    try:
        response = await client.get("/models", timeout=15)
    except httpx.TimeoutException:
        return "timeout"
    except httpx.TransportError:
        return "unreachable"
    if response.status_code in (401, 403):
        return "unauthorized"
    if response.status_code != 200:
        return f"http_{response.status_code}"
    try:
        offered = {str(m["id"]) for m in response.json()["data"]}
    except (ValueError, KeyError, TypeError):
        return "bad_response"
    return "ok" if model in offered else "model_unavailable"
