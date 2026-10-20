"""The OpenAI adapter and its settings: what every request asks of the model."""

import json

import httpx
import pytest
from witx.config import Settings
from witx.models import ModelError, ModelRequest
from witx.models.chat import ChatCompletions
from witx.models.openai import OpenAIResponses


async def test_a_request_with_tools_leaves_the_call_to_the_model_at_the_set_effort() -> None:
    sent: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r1", "status": "completed", "output": []})

    model = OpenAIResponses("key", transport=httpx.MockTransport(handler))
    tool = {"type": "function", "name": "complete_task", "parameters": {}}
    await model.respond(
        ModelRequest(model="m", instructions="i", input=[], tools=[tool], allowed_providers=None)
    )
    assert "tool_choice" not in sent[0]  # forcing one is refused by Claude, spun others
    assert sent[0]["reasoning"] == {"effort": "medium"}


def test_a_request_without_tools_requires_none_on_either_provider() -> None:
    # A side chat answers in words: requiring a tool with no tools is refused (400).
    words = ModelRequest(model="m", instructions="i", input=[], tools=[], allowed_providers=None)
    responses = OpenAIResponses("key").body(words)
    chat = ChatCompletions("key", base_url="https://chat.example/v1").body(words)
    assert "tool_choice" not in responses
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & chat.keys()


async def test_a_response_cut_off_for_writing_without_a_tool_call_is_asked_again() -> None:
    def cut_off(reason: str):  # type: ignore[no-untyped-def]
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "r",
                    "status": "incomplete",
                    "incomplete_details": {"reason": reason},
                    "output": [{"type": "reasoning", "summary": []}] * 3,
                },
            )

        return OpenAIResponses("key", transport=httpx.MockTransport(handler))

    request = ModelRequest(model="m", instructions="i", input=[], tools=[], allowed_providers=None)
    with pytest.raises(ModelError) as looping:
        await cut_off("max_messages").respond(request)
    assert looping.value.retryable
    assert looping.value.raw is not None and len(looping.value.raw["output"]) == 3
    with pytest.raises(ModelError) as too_long:
        await cut_off("max_output_tokens").respond(request)
    assert not too_long.value.retryable


def test_the_default_reasoning_effort_cannot_be_set_below_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It is the risk check's too, whatever a model allows a person to choose.
    monkeypatch.setenv("WITX_DATABASE_URL", "postgresql://x@localhost/witx")
    assert Settings.from_env().reasoning_effort == "medium"
    monkeypatch.setenv("WITX_REASONING_EFFORT", "high")
    assert Settings.from_env().reasoning_effort == "high"
    for lower in ("low", "none", "minimal"):
        monkeypatch.setenv("WITX_REASONING_EFFORT", lower)
        with pytest.raises(ValueError):
            Settings.from_env()


async def test_the_agent_may_search_the_web_and_the_risk_check_may_not() -> None:
    sent: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r", "output": [], "usage": {}})

    model = OpenAIResponses("key", transport=httpx.MockTransport(handler))
    tool = {"type": "function", "name": "complete_task", "parameters": {}}
    await model.respond(
        ModelRequest(
            model="m",
            instructions="i",
            input=[],
            tools=[tool],
            allowed_providers=None,
            web_search=True,
        )
    )
    await model.respond(
        ModelRequest(model="m", instructions="i", input=[], tools=[tool], allowed_providers=None)
    )
    assert {"type": "web_search"} in sent[0]["tools"]
    assert {"type": "web_search"} not in sent[1]["tools"]


async def test_a_task_s_reasoning_effort_overrides_the_default() -> None:
    sent: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r", "output": [], "usage": {}})

    model = OpenAIResponses("key", transport=httpx.MockTransport(handler))
    request = ModelRequest(model="m", instructions="i", input=[], tools=[], allowed_providers=None)
    await model.respond(request)
    await model.respond(ModelRequest(**{**request.__dict__, "reasoning_effort": "xhigh"}))
    assert [s["reasoning"]["effort"] for s in sent] == ["medium", "xhigh"]


def listing(status: int, ids: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith("/models")
        return httpx.Response(status, json={"data": [{"id": i} for i in ids]})

    return httpx.MockTransport(handler)


async def test_a_model_is_ready_when_the_provider_accepts_the_key_and_offers_it() -> None:
    def client(status: int, ids: list[str]) -> OpenAIResponses:
        return OpenAIResponses("k", base_url="https://api.test/v1", transport=listing(status, ids))

    assert await client(200, ["gpt-6-luna"]).ready("gpt-6-luna") == "ok"
    assert await client(200, ["gpt-5"]).ready("gpt-6-luna") == "model_unavailable"
    assert await client(401, []).ready("gpt-6-luna") == "unauthorized"
    assert await client(503, []).ready("gpt-6-luna") == "http_503"


async def test_a_watched_call_streams_the_agents_words_and_acts_on_the_whole_response() -> None:
    sent: list[dict] = []  # type: ignore[type-arg]
    completed = {
        "id": "r2",
        "status": "completed",
        "output": [{"type": "function_call", "name": "complete_task", "arguments": "{}"}],
        "usage": {"output_tokens": 9},
    }
    call = {"type": "function_call", "name": "complete_task", "arguments": ""}
    events = [
        ("response.created", {"response": {"id": "r2"}}),
        ("response.reasoning_summary_text.delta", {"delta": "**Thinking**"}),  # not words
        ("response.output_text.delta", {"output_index": 1, "delta": "It is "}),
        ("response.output_text.delta", {"output_index": 1, "delta": "**8:07**."}),
        ("response.output_item.added", {"output_index": 2, "item": call}),
        (
            "response.function_call_arguments.delta",
            {"output_index": 2, "delta": '{"summary": "Sett'},
        ),
        (
            "response.function_call_arguments.delta",
            {"output_index": 2, "delta": 'ings is \\"open\\"\\'},
        ),
        ("response.function_call_arguments.delta", {"output_index": 2, "delta": 'n.", "evidence'}),
        ("response.completed", {"response": completed}),
    ]
    body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    heard: list[tuple[str, str]] = []

    async def listen(text: str, phase: str) -> None:
        heard.append((text, phase))

    model = OpenAIResponses("key", transport=httpx.MockTransport(handler))
    tool = {"type": "function", "name": "complete_task", "parameters": {}}
    response = await model.respond(
        ModelRequest(
            model="m",
            instructions="i",
            input=[],
            tools=[tool],
            allowed_providers=None,
            on_text=listen,
        )
    )
    assert sent[0]["stream"] is True
    assert heard == [
        ("It is", "talking"),
        ("It is **8:07**.", "talking"),
        ("It is **8:07**.", "planning"),  # a call began: the model plans its next input
        ("It is **8:07**.\n\nSett", "planning"),
        ('It is **8:07**.\n\nSettings is "open"', "planning"),  # a cut-off escape waits
        ('It is **8:07**.\n\nSettings is "open"\n.', "planning"),
    ]
    assert (response.id, response.output, response.usage) == (
        "r2",
        completed["output"],
        completed["usage"],
    )


async def test_an_unwatched_call_is_not_streamed() -> None:
    sent: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r1", "status": "completed", "output": []})

    model = OpenAIResponses("key", transport=httpx.MockTransport(handler))
    await model.respond(
        ModelRequest(model="m", instructions="i", input=[], tools=[], allowed_providers=None)
    )
    assert "stream" not in sent[0] and "summary" not in sent[0]["reasoning"]
