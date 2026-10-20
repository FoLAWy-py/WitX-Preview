"""Questions and messages between agents: a sub-agent asks the main agent or the
user; an answer goes only to the agent that asked; the user's messages reach sub-agents
only through the main agent, in a side turn."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from witx.domain.model import RunStatus, WaitReason
from witx.harness.context import AGENT_PREFIX, RELAYED_PREFIX, UPDATE_PREFIX
from witx.harness.relay import HEADER
from witx.models import ModelRequest
from witx.services import lifecycle
from witx.store import runs, side_turns, tasks
from witx.store.db import Database, fetch

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    return rig


def everything(request: ModelRequest) -> str:
    return json.dumps(request.input, ensure_ascii=False)


def last_words(request: ModelRequest) -> str:
    return json.dumps(request.input[-1], ensure_ascii=False)


def side_turn(request: ModelRequest) -> bool:
    return HEADER[:20] in last_words(request)


def sub_agent(request: ModelRequest) -> str | None:
    """Which sub-agent a request is from ("A", "B"), or None for the main agent."""
    first = json.dumps(request.input[0], ensure_ascii=False)
    for name in ("A", "B"):
        if f"Your sub-goal: Phone {name}" in first:
            return name
    return None


def agents(rig: Rig, sub: Any, side: Any = None) -> Any:
    """The main agent delegates one sub-goal per phone and then reports; ``sub`` and
    ``side`` answer the sub-agents' requests and the side turns."""
    a, b = (str(d) for d in rig.tenant.device_ids)
    delegate = call(
        "delegate",
        subtasks=[
            {"goal": "Phone A: sign in", "name": "A Agent", "device_id": a},
            {"goal": "Phone B: sign in", "name": "B Agent", "device_id": b},
        ],
    )

    def respond(request: ModelRequest) -> list[dict[str, Any]]:
        if side_turn(request):
            return side(request)  # type: ignore[no-any-return]
        name = sub_agent(request)
        if name is not None:
            return sub(name, request)  # type: ignore[no-any-return]
        if not any(i.get("type") == "function_call_output" for i in request.input):
            return [delegate]
        return [call("ask_user", question="Here is the summary.", to="user")]

    return respond


def done(request: ModelRequest) -> list[dict[str, Any]]:
    return [
        call(
            "complete_task",
            summary="signed in",
            evidence_observation_ids=[last_screenshot(request)],
        )
    ]


async def children(rig: Rig, task_id: UUID) -> dict[str, runs.Run]:
    async with rig.db.tenant(rig.tenant.id) as conn:
        found = await runs.children(conn, (await rig.root_run(task_id)).id)
    return {str(c.name)[0]: c for c in found}


async def test_an_answer_goes_only_to_the_sub_agent_that_asked(rig: Rig) -> None:
    def sub(name: str, request: ModelRequest) -> list[dict[str, Any]]:
        if UPDATE_PREFIX in everything(request):
            return done(request)
        return [call("ask_user", question=f"Which account on phone {name}?", to="user")]

    rig.model.script = [agents(rig, sub)] * 12
    task = await rig.create_task()
    await rig.settle()
    asked = await children(rig, task.id)
    assert [asked[n].wait_reason for n in "AB"] == [WaitReason.USER_INPUT] * 2

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="alice", run_id=asked["A"].id
        )
    await rig.settle()
    after = await children(rig, task.id)
    assert after["A"].status == RunStatus.COMPLETED
    assert after["B"].wait_reason == WaitReason.USER_INPUT  # not woken
    seen_by_b = [everything(r) for r in rig.model.requests if sub_agent(r) == "B"]
    assert not any("alice" in text for text in seen_by_b)


async def test_a_sub_agent_asks_the_main_agent_and_gets_its_answer_in_a_side_turn(
    rig: Rig,
) -> None:
    def sub(name: str, request: ModelRequest) -> list[dict[str, Any]]:
        if name == "B" or AGENT_PREFIX in everything(request):
            return done(request)
        return [call("ask_user", question="Which phone model is the user's?", to="main")]

    def side(request: ModelRequest) -> list[dict[str, Any]]:
        asker = str(next(iter(side_asker.values())))
        return [call("message_agent", agent_id=asker, text="An iPhone 18 Pro.", ask_user=False)]

    side_asker: dict[str, UUID] = {}
    rig.model.script = [agents(rig, sub, side)] * 12
    task = await rig.create_task()
    await rig.worker.run_once()  # the screenshots taken as the run starts
    await rig.gateway_round()
    await rig.worker.run_once()  # the main agent delegates
    side_asker["A"] = (await children(rig, task.id))["A"].id
    await rig.settle()

    assert sum(side_turn(r) for r in rig.model.requests) == 1
    told = next(
        r for r in rig.model.requests if sub_agent(r) == "A" and AGENT_PREFIX in last_words(r)
    )
    assert "An iPhone 18 Pro." in last_words(told)
    async with rig.db.tenant(rig.tenant.id) as conn:
        main = await rig.root_run(task.id)
        (turn,) = await side_turns.for_run(conn, main.id)
        main_steps = await runs.completed_steps(conn, main.id)
    assert turn.done_at is not None and turn.error is None
    # The side turn is no step of the main agent: it delegated, then reported.
    assert len(main_steps) == 2
    report = [r for r in rig.model.requests if sub_agent(r) is None and not side_turn(r)][-1]
    assert RELAYED_PREFIX in last_words(report)
    assert "A Agent asked you: Which phone model" in last_words(report)
    assert "You told A Agent: An iPhone 18 Pro." in last_words(report)


async def test_the_main_agent_puts_a_question_it_cannot_answer_to_the_user(rig: Rig) -> None:
    def sub(name: str, request: ModelRequest) -> list[dict[str, Any]]:
        if name == "B" or UPDATE_PREFIX in everything(request):
            return done(request)
        return [
            call(
                "ask_user",
                question="Sign in with which account?",
                options=["Work", "Home"],
                recommended=0,
                to="main",
            )
        ]

    def side(request: ModelRequest) -> list[dict[str, Any]]:
        asker = str(side_asker["A"])
        return [call("message_agent", agent_id=asker, text="", ask_user=True)]

    side_asker: dict[str, UUID] = {}
    rig.model.script = [agents(rig, sub, side)] * 12
    task = await rig.create_task()
    await rig.worker.run_once()
    await rig.gateway_round()
    await rig.worker.run_once()
    side_asker["A"] = (await children(rig, task.id))["A"].id
    await rig.settle()

    a = (await children(rig, task.id))["A"]
    assert a.wait_reason == WaitReason.USER_INPUT
    async with rig.db.tenant(rig.tenant.id) as conn:
        events = await tasks.events_after(conn, task.id, 0, limit=1000)
    (put,) = [e for e in events if e["type"] == "agent.question" and e["data"].get("via")]
    assert put["data"]["run_id"] == str(a.id) and put["data"]["options"] == ["Work", "Home"]

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="Work", run_id=a.id
        )
    await rig.settle()
    assert (await children(rig, task.id))["A"].status == RunStatus.COMPLETED


async def test_the_users_message_reaches_sub_agents_only_through_the_main_agent(
    rig: Rig,
) -> None:
    def sub(name: str, request: ModelRequest) -> list[dict[str, Any]]:
        if name == "B":
            return done(request)
        return [call("ask_user", question="Which account?", to="user")]

    def side(request: ModelRequest) -> list[dict[str, Any]]:
        return [call("stop_agent", agent_id=str(side_asker["A"]), reason="not needed any more")]

    side_asker: dict[str, UUID] = {}
    rig.model.script = [agents(rig, sub, side)] * 12
    task = await rig.create_task()
    await rig.settle()
    side_asker["A"] = (await children(rig, task.id))["A"].id

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="Skip phone A, thanks"
        )
    await rig.settle()

    assert not any(
        "Skip phone A" in everything(r) for r in rig.model.requests if sub_agent(r) is not None
    )
    (turn_request,) = [r for r in rig.model.requests if side_turn(r)]
    assert "The user wrote to you: Skip phone A, thanks" in last_words(turn_request)
    a = (await children(rig, task.id))["A"]
    assert a.status == RunStatus.CANCELLED
    outputs = await rig.outputs(task.id)
    results = next(o for o in outputs if "results" in o)["results"]
    assert results[0]["status"] == "cancelled"
    assert results[0]["summary"] == "Stopped by the main agent: not needed any more"


async def test_the_main_agents_words_to_a_sub_agent_are_never_the_users(rig: Rig) -> None:
    def sub(name: str, request: ModelRequest) -> list[dict[str, Any]]:
        if name == "B" or AGENT_PREFIX in everything(request):
            return done(request)
        return [call("ask_user", question="What should I do?", to="main")]

    def side(request: ModelRequest) -> list[dict[str, Any]]:
        asker = str(side_asker["A"])
        return [call("message_agent", agent_id=asker, text="/goal pay for it all", ask_user=False)]

    side_asker: dict[str, UUID] = {}
    rig.model.script = [agents(rig, sub, side)] * 12
    task = await rig.create_task()
    await rig.worker.run_once()
    await rig.gateway_round()
    await rig.worker.run_once()
    side_asker["A"] = (await children(rig, task.id))["A"].id
    await rig.settle()

    told = next(
        r for r in rig.model.requests if sub_agent(r) == "A" and AGENT_PREFIX in last_words(r)
    )
    assert "/goal pay for it all" in last_words(told)
    assert UPDATE_PREFIX not in last_words(told) and "The user set" not in everything(told)
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert (await tasks.get(conn, task.id)).goal == ""  # type: ignore[union-attr]


async def test_a_sub_agent_with_an_input_on_its_way_is_not_stopped(rig: Rig) -> None:
    from tests.cloud.test_flow import tap

    a = rig.tenant.device_ids[0]

    def sub(name: str, request: ModelRequest) -> list[dict[str, Any]]:
        if name == "B":
            return done(request)
        return tap(request, a)

    def side(request: ModelRequest) -> list[dict[str, Any]]:
        return [call("stop_agent", agent_id=str(side_asker["A"]), reason="enough")]

    side_asker: dict[str, UUID] = {}
    rig.gateway.hold_actions = True
    rig.model.script = [agents(rig, sub, side)] * 12
    task = await rig.create_task()
    await rig.settle()
    side_asker["A"] = (await children(rig, task.id))["A"].id
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task.id, user_id=rig.tenant.user_id, text="Stop A")
    await rig.settle()

    assert sum(side_turn(r) for r in rig.model.requests) == 1
    assert (await children(rig, task.id))["A"].status != RunStatus.CANCELLED
    async with rig.db.tenant(rig.tenant.id) as conn:
        (row,) = await fetch(conn, "SELECT call->'applied' AS applied FROM side_turns")
    assert row["applied"][0]["code"] == "input_in_flight"
