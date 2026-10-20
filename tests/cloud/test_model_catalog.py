"""The model catalog, the Chat Completions adapter, and switching a task's model."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from witx.config import CatalogModel, Limits, Settings, parse_models
from witx.domain import rules
from witx.domain.model import RunStatus, TaskStatus, WaitReason
from witx.domain.rules import Rejected
from witx.models import ModelError, ModelRequest, ModelResponse
from witx.models.chat import ChatCompletions, messages, output_items
from witx.models.router import ModelRouter
from witx.services import lifecycle
from witx.store import identity as identity_store
from witx.store import runs
from witx.store.db import Database, fetch

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete, tap

IMAGE = "data:image/png;base64,iVBORw0KGgo="


# ---------------------------------------------------------------- catalog


def test_the_catalog_names_each_model_and_where_it_runs() -> None:
    models = parse_models("gpt-6-luna=openai, flash=deepinfra:deepseek-ai/DeepSeek-V4.1-Flash")
    assert models == {
        "gpt-6-luna": CatalogModel("gpt-6-luna", "openai", "gpt-6-luna"),
        "flash": CatalogModel("flash", "deepinfra", "deepseek-ai/DeepSeek-V4.1-Flash"),
    }
    with pytest.raises(ValueError):
        parse_models("flash=somewhere-else:x")


def test_the_default_model_must_be_in_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WITX_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("WITX_MODELS", "flash=deepinfra:deepseek-ai/DeepSeek-V4.1-Flash")
    monkeypatch.setenv("WITX_DEFAULT_MODEL", "gpt-6-luna")
    with pytest.raises(ValueError, match="WITX_DEFAULT_MODEL"):
        Settings.from_env()
    monkeypatch.setenv("WITX_DEFAULT_MODEL", "flash")
    assert set(Settings.from_env().catalog()) == {"flash"}


# ---------------------------------------------------------------- Chat Completions


def test_responses_history_becomes_chat_messages_with_tool_calls_grouped_and_images_kept() -> None:
    history = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Open Wi-Fi"},
                {"type": "input_image", "image_url": IMAGE, "detail": "high"},
            ],
        },
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Tapping it."}],
        },
        {"type": "function_call", "call_id": "c1", "name": "tap", "arguments": '{"x": 1}'},
        {"type": "function_call_output", "call_id": "c1", "output": '{"status": "done"}'},
    ]
    assert messages("Be careful.", history) == [
        {"role": "system", "content": "Be careful."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Open Wi-Fi"},
                {"type": "image_url", "image_url": {"url": IMAGE}},
            ],
        },
        {
            "role": "assistant",
            "content": "Tapping it.",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "tap", "arguments": '{"x": 1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"status": "done"}'},
    ]


def test_a_chat_reply_becomes_responses_items() -> None:
    reply = {
        "content": "Done.",
        "tool_calls": [
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "complete_task", "arguments": "{}"},
            }
        ],
    }
    assert output_items(reply) == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Done."}],
        },
        {"type": "function_call", "call_id": "c2", "name": "complete_task", "arguments": "{}"},
    ]


def chat_client(handler: Any, effort: str = "medium") -> ChatCompletions:
    return ChatCompletions(
        "key",
        base_url="https://chat.example/v1",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )


REQUEST = ModelRequest(
    model="deepseek-ai/DeepSeek-V4.1-Flash",
    instructions="Be careful.",
    input=[{"type": "message", "role": "user", "content": "hi"}],
    tools=[{"type": "function", "name": "complete_task", "parameters": {"type": "object"}}],
    allowed_providers=("openai", "deepinfra"),
)


async def test_every_chat_turn_must_call_a_tool_and_usage_uses_the_shared_keys() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "complete_task", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127},
            },
        )

    response = await chat_client(handler, effort="xhigh").respond(REQUEST)
    (body,) = sent
    assert "tool_choice" not in body and body["parallel_tool_calls"] is False
    assert body["reasoning_effort"] == "high"  # the highest Chat Completions knows
    assert body["tools"][0]["function"]["name"] == "complete_task"
    assert response.output[0]["name"] == "complete_task"
    assert response.usage == {"input_tokens": 120, "output_tokens": 7, "total_tokens": 127}


async def test_chat_errors_are_retryable_only_when_the_provider_may_recover() -> None:
    def busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    def truncated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {}}]})

    with pytest.raises(ModelError) as rate_limited:
        await chat_client(busy).respond(REQUEST)
    assert rate_limited.value.retryable and "slow down" in str(rate_limited.value)
    with pytest.raises(ModelError) as cut_off:
        await chat_client(truncated).respond(REQUEST)
    assert cut_off.value.code == "incomplete" and not cut_off.value.retryable


# ---------------------------------------------------------------- router


class Recorder:
    def __init__(self) -> None:
        self.models: list[str] = []

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.models.append(request.model)
        return ModelResponse(id="r", output=[], usage=None, raw={})


async def test_each_request_goes_to_its_models_provider_under_the_providers_id() -> None:
    openai, deepinfra = Recorder(), Recorder()
    router = ModelRouter(
        parse_models("gpt-6-luna=openai,flash=deepinfra:deepseek-ai/DeepSeek-V4.1-Flash"),
        {"openai": openai, "deepinfra": deepinfra},
        "gpt-6-luna",
    )
    await router.respond(REQUEST.__class__(**{**REQUEST.__dict__, "model": "flash"}))
    await router.respond(REQUEST.__class__(**{**REQUEST.__dict__, "model": "gpt-6-luna"}))
    assert deepinfra.models == ["deepseek-ai/DeepSeek-V4.1-Flash"]
    assert openai.models == ["gpt-6-luna"]
    with pytest.raises(ModelError, match="unknown_model"):
        await router.respond(REQUEST.__class__(**{**REQUEST.__dict__, "model": "gpt-4"}))


def test_until_an_owner_chooses_only_the_default_models_provider_is_allowed() -> None:
    assert rules.allowed_providers(None, "openai") == {"openai"}
    assert rules.allowed_providers(["deepinfra"], "openai") == {"deepinfra"}


def test_an_owner_allows_at_least_one_provider_of_the_catalog() -> None:
    assert rules.check_provider_choice(["openai", "openai"], {"openai"}) == ["openai"]
    for chosen in ([], ["anthropic"]):
        with pytest.raises(rules.Rejected, match="invalid_providers"):
            rules.check_provider_choice(chosen, {"openai", "deepinfra"})


async def test_nothing_reaches_a_provider_the_tenant_does_not_allow() -> None:
    openai, deepinfra = Recorder(), Recorder()
    router = ModelRouter(
        parse_models("gpt-6-luna=openai,flash=deepinfra:deepseek-ai/DeepSeek-V4.1-Flash"),
        {"openai": openai, "deepinfra": deepinfra},
        "gpt-6-luna",
    )
    for chosen in (None, ("openai",)):  # no choice yet: only the default model's provider
        request = REQUEST.__class__(
            **{**REQUEST.__dict__, "model": "flash", "allowed_providers": chosen}
        )
        with pytest.raises(ModelError, match="provider_not_allowed") as error:
            await router.respond(request)
        assert not error.value.retryable
    assert deepinfra.models == []


# ---------------------------------------------------------------- switching a task's model

CATALOG = parse_models("test-model=openai,flash=deepinfra:deepseek-ai/DeepSeek-V4.1-Flash")


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def test_a_task_switches_model_and_its_risk_check_between_steps_keeping_its_history(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        [call("ask_user", question="Which network?")],
    ]
    task = await rig.create_task()
    await rig.settle()  # the run now waits for the answer

    async with rig.db.tenant(rig.tenant.id) as conn:
        switched = await lifecycle.set_model(conn, task_id=task.id, model="flash", catalog=CATALOG)
        with pytest.raises(Rejected, match="unknown_model"):
            await lifecycle.set_model(conn, task_id=task.id, model="gpt-4", catalog=CATALOG)
    assert switched.model == "flash"
    rig.model.script = [lambda r: tap(r, device), complete]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task.id, user_id=rig.tenant.user_id, text="Home")
    await rig.settle()

    models = ["test-model", "test-model", "flash", "flash"]
    assert [r.model for r in rig.model.requests] == models
    # The risk check of the tap ran on the task's new model too.
    assert [c["model"] for c in rig.risk.calls] == ["flash"]
    first, later = rig.model.requests[0], rig.model.requests[2]
    assert len(later.input) > len(first.input)  # the earlier steps are still there
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    async with rig.db.tenant(rig.tenant.id) as conn:
        rows = await fetch(conn, "SELECT model FROM steps ORDER BY started_at")
    assert [r["model"] for r in rows] == models
    assert "task.model" in await rig.event_types(task.id)


async def test_a_run_stopped_by_a_model_error_tries_again_with_the_new_model(rig: Rig) -> None:
    def refused(request: ModelRequest) -> list[dict[str, Any]]:
        raise ModelError("http_400", "model does not support images", retryable=False)

    rig.model.script = [refused]
    task = await rig.create_task()
    await rig.settle()
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason) == (RunStatus.WAITING, WaitReason.USER_INPUT)

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.set_model(conn, task_id=task.id, model="flash", catalog=CATALOG)
        assert (await runs.get(conn, run.id)).status == RunStatus.RUNNABLE  # type: ignore[union-attr]
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], complete]
    await rig.settle()
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    assert [r.model for r in rig.model.requests] == ["test-model", "flash", "flash"]


async def test_a_failed_step_keeps_what_the_provider_answered_and_is_tried_again(
    rig: Rig,
) -> None:
    answered = {"id": "r", "status": "incomplete", "output": [{"type": "reasoning"}] * 101}

    def looping(request: ModelRequest) -> list[dict[str, Any]]:
        raise ModelError("incomplete", "max_messages", retryable=True, raw=answered)

    rig.model.script = [looping]
    task = await rig.create_task()
    await rig.settle()
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason) == (RunStatus.WAITING, WaitReason.MODEL_RETRY)
    async with rig.db.tenant(rig.tenant.id) as conn:
        rows = await fetch(conn, "SELECT status, response FROM steps WHERE run_id = :r", r=run.id)
        events = await fetch(
            conn,
            "SELECT data FROM events WHERE task_id = :t AND type = 'run.model_error'",
            t=task.id,
        )
    assert [(r["status"], len(r["response"]["output"])) for r in rows] == [("error", 101)]
    assert [e["data"]["retrying"] for e in events] == [True]


async def test_every_model_call_of_a_task_carries_the_tenants_choice_of_providers(
    rig: Rig, admin_db: Database
) -> None:
    async with admin_db.system() as conn:
        await identity_store.set_allowed_providers(conn, rig.tenant.id, ["deepinfra"])
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], complete]
    await rig.create_task("Open Settings")
    await rig.settle()
    assert rig.model.requests
    assert {r.allowed_providers for r in rig.model.requests} == {("deepinfra",)}


def test_a_task_falls_back_to_the_next_allowed_model_that_has_not_failed() -> None:
    catalog = [("luna", "openai"), ("flash", "deepinfra"), ("mini", "openai")]

    def pick(current: str, failed: set[str], allowed: set[str]) -> str | None:
        return rules.fallback_model(
            current=current, failed=failed, catalog=catalog, allowed=allowed
        )

    assert pick("luna", {"luna"}, {"openai", "deepinfra"}) == "flash"
    assert pick("luna", {"luna"}, {"openai"}) == "mini"  # deepinfra is not allowed
    assert pick("flash", {"luna", "flash"}, {"openai", "deepinfra"}) == "mini"
    assert pick("mini", {"luna", "flash", "mini"}, {"openai", "deepinfra"}) is None


FALLBACK_CATALOG = "test-model=openai,flash=deepinfra:flash-id"


async def falling_back(
    db: Database, admin_db: Database, tmp_path: Path, *, on: bool, retryable: bool = True
) -> tuple[Rig, Any]:
    """A task whose model fails at once, in a tenant that allows both providers."""
    tenant = await provision(admin_db, "acme", devices=1)
    async with admin_db.system() as conn:
        await identity_store.set_allowed_providers(conn, tenant.id, ["deepinfra", "openai"])
        await identity_store.set_model_fallback(conn, tenant.id, on)
    rig = Rig(db, tenant, tmp_path, Limits(model_retries=1), catalog=FALLBACK_CATALOG)
    await rig.connect([device_info(0)])

    def failing(request: ModelRequest) -> list[dict[str, Any]]:
        raise ModelError("http_429", "rate limited", retryable=retryable)

    device = tenant.device_ids[0]
    rig.model.script = [failing, [call("observe", device_id=device)], complete]
    task = await rig.create_task()
    await rig.settle()
    return rig, task


async def test_a_model_that_keeps_failing_is_replaced_by_the_next_allowed_one(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task = await falling_back(db, admin_db, tmp_path, on=True)
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    assert [r.model for r in rig.model.requests] == ["test-model", "flash", "flash"]
    async with rig.db.tenant(rig.tenant.id) as conn:
        events = await fetch(
            conn,
            "SELECT data FROM events WHERE task_id = :t AND type = 'task.model'",
            t=task.id,
        )
    assert [e["data"] for e in events] == [
        {"model": "flash", "previous": "test-model", "fallback_for": "http_429"}
    ]


async def test_without_the_owners_consent_a_failing_task_waits_for_a_person(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task = await falling_back(db, admin_db, tmp_path, on=False)
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason) == (RunStatus.WAITING, WaitReason.USER_INPUT)
    assert (await rig.task(task.id)).model == "test-model"


async def test_an_error_no_other_model_would_fix_never_causes_a_switch(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task = await falling_back(db, admin_db, tmp_path, on=True, retryable=False)
    assert (await rig.task(task.id)).model == "test-model"
    assert [r.model for r in rig.model.requests] == ["test-model"]


def test_a_model_may_let_a_person_choose_a_lower_effort_than_medium() -> None:
    models = parse_models(
        "gpt-6-luna=openai,gpt-6.1-sol=openai@low,opus=deepinfra:anthropic/claude-opus-5-5@low"
    )
    assert models["gpt-6-luna"].efforts == ("medium", "high", "xhigh", "max")
    assert models["gpt-6.1-sol"].efforts == ("low", "medium", "high", "xhigh", "max")
    assert models["opus"].provider_model == "anthropic/claude-opus-5-5"
    with pytest.raises(ValueError):
        parse_models("gpt-6.1-sol=openai@none")


async def test_claude_through_chat_completions_gets_every_effort_as_it_is() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "c", "choices": [{"message": {}}]})

    claude = replace(REQUEST, model="anthropic/claude-opus-5-5")
    for effort in ("low", "xhigh", "max"):
        await chat_client(handler).respond(replace(claude, reasoning_effort=effort))
    await chat_client(handler).respond(replace(REQUEST, reasoning_effort="max"))
    assert [b["reasoning_effort"] for b in sent] == ["low", "xhigh", "max", "high"]
