"""Messages sent while the main agent works: they wait queued until it reads them,
can be withdrawn until then, and can be made to reach it now (steer)."""

import json
from pathlib import Path
from typing import Any

import pytest
from witx.api import views
from witx.domain.model import ActionStatus, RiskVerdict, Task
from witx.domain.rules import Rejected
from witx.models import ModelRequest
from witx.services import lifecycle
from witx.store import tasks
from witx.store.db import Database, fetch

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import tap


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


def seen(request: ModelRequest) -> str:
    return json.dumps(request.input, ensure_ascii=False)


async def waiting_for_approval(rig: Rig) -> Task:
    """The main agent proposed a tap that waits for the person's approval."""
    device = rig.tenant.device_ids[0]
    rig.risk.verdict = RiskVerdict(sensitive=True, effects=["payment"], reason="Pay button")
    rig.model.script = [
        lambda r: tap(r, device),
        lambda r: [call("ask_user", question="What now?", to="user")],
    ]
    task = await rig.create_task()
    await rig.settle()
    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.AWAITING_APPROVAL
    return task


async def write(rig: Rig, task: Task, text: str) -> None:
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task.id, user_id=rig.tenant.user_id, text=text)


async def queued(rig: Rig, task: Task) -> list[str]:
    async with rig.db.tenant(rig.tenant.id) as conn:
        detail = await views.task_detail(conn, await tasks.get(conn, task.id))  # type: ignore[arg-type]
    return [i.text for i in detail.inputs if i.queued]


async def test_a_message_waits_queued_and_steering_makes_the_main_agent_read_it_now(
    rig: Rig,
) -> None:
    task = await waiting_for_approval(rig)
    await write(rig, task, "Use the other card")
    await rig.settle()
    assert await queued(rig, task) == ["Use the other card"]  # it still waits for approval

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.steer(conn, task_id=task.id)
    await rig.settle()

    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.CANCELLED
    assert "Use the other card" in seen(rig.model.requests[-1])
    assert await queued(rig, task) == []


async def test_a_queued_message_can_be_withdrawn_but_not_once_it_was_read(rig: Rig) -> None:
    task = await waiting_for_approval(rig)
    await write(rig, task, "Actually, never mind")
    async with rig.db.tenant(rig.tenant.id) as conn:
        (seq,) = [i["seq"] for i in await tasks.inputs(conn, task.id) if i["seq"] > 1]
        await lifecycle.withdraw_input(conn, task_id=task.id, seq=seq, user_id=rig.tenant.user_id)
    await write(rig, task, "Go on")
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.steer(conn, task_id=task.id)
    await rig.settle()

    assert all("never mind" not in seen(r) for r in rig.model.requests)
    async with rig.db.tenant(rig.tenant.id) as conn:
        (read,) = [i["seq"] for i in await tasks.inputs(conn, task.id) if i["text"] == "Go on"]
        with pytest.raises(Rejected) as refused:
            await lifecycle.withdraw_input(
                conn, task_id=task.id, seq=read, user_id=rig.tenant.user_id
            )
    assert refused.value.code == "already_read"


async def test_steering_drops_a_model_call_under_way_and_carries_out_nothing_of_it(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    task_box: list[Task] = []
    respond = rig.model.respond

    async def steered_while_thinking(request: ModelRequest) -> Any:
        # The person writes and steers while this model call is under way.
        if not rig.model.requests:
            await write(rig, task_box[0], "Stop, use the other card")
            async with rig.db.tenant(rig.tenant.id) as conn:
                await lifecycle.steer(conn, task_id=task_box[0].id)
        return await respond(request)

    rig.model.respond = steered_while_thinking  # type: ignore[method-assign]
    rig.model.script = [
        lambda r: tap(r, device),  # dropped
        lambda r: [call("ask_user", question="Which card?", to="user")],
    ]
    task_box.append(await rig.create_task())
    await rig.settle()

    assert await rig.actions(task_box[0].id) == []  # the dropped tap was never admitted
    assert "Stop, use the other card" in seen(rig.model.requests[-1])
    async with rig.db.tenant(rig.tenant.id) as conn:
        statuses = [
            r["status"]
            for r in await fetch(
                conn,
                "SELECT s.status FROM steps s JOIN runs r ON r.id = s.run_id"
                " WHERE r.task_id = :t ORDER BY s.started_at",
                t=task_box[0].id,
            )
        ]
    assert statuses == ["abandoned", "done"]


async def test_steering_with_nothing_queued_changes_nothing(rig: Rig) -> None:
    task = await waiting_for_approval(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.steer(conn, task_id=task.id)
    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.AWAITING_APPROVAL


async def test_a_withdrawn_messages_number_is_not_given_again(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("delegate", subtasks=[{"goal": "Sign in", "name": "A", "device_id": str(device)}])],
        lambda r: [call("ask_user", question="Which account?", to="user")],
    ]
    task = await rig.create_task()
    await rig.settle()
    # The main agent waits for its sub-agent: the message waits for a side turn.
    await write(rig, task, "Never mind")
    async with rig.db.tenant(rig.tenant.id) as conn:
        withdrawn = max(i["seq"] for i in await tasks.inputs(conn, task.id))
        await lifecycle.withdraw_input(
            conn, task_id=task.id, seq=withdrawn, user_id=rig.tenant.user_id
        )
    await write(rig, task, "Go on")
    async with rig.db.tenant(rig.tenant.id) as conn:
        newest = max(i["seq"] for i in await tasks.inputs(conn, task.id))
    assert newest == withdrawn + 1


async def test_a_message_that_came_while_the_agent_finished_starts_the_next_round(
    rig: Rig,
) -> None:
    task_box: list[Task] = []
    respond = rig.model.respond

    async def written_while_finishing(request: ModelRequest) -> Any:
        if not rig.model.requests:
            await write(rig, task_box[0], "Also check the battery")
        return await respond(request)

    rig.model.respond = written_while_finishing  # type: ignore[method-assign]
    rig.model.script = [
        lambda r: [
            call("complete_task", summary="done", evidence_observation_ids=[last_screenshot(r)])
        ],
        lambda r: [call("ask_user", question="Battery is fine. Anything else?", to="user")],
    ]
    task_box.append(await rig.create_task())
    await rig.settle()

    assert "Also check the battery" in seen(rig.model.requests[-1])
    assert len(rig.model.requests) == 2
