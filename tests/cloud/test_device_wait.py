"""A run whose phones cannot be used (offline, or connected but not responding) waits for
one to come back without calling the model, and gives up after a while."""

from pathlib import Path
from typing import Any

import pytest
from witx.domain.model import RunStatus, TaskStatus, WaitReason
from witx.services import lifecycle
from witx.store import runs
from witx.store.db import Database, execute
from witx_contracts import gateway as wire

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision

NOT_RESPONDING = {"problems": ["not_responding"]}


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    return rig


async def report(rig: Rig, first: dict[str, Any], second: dict[str, Any] | None = None) -> None:
    """What the Gateway says about its two phones; an empty dict: nothing is wrong."""
    infos = [device_info(0) | first, device_info(1) | (second or {})]
    await rig.handle(wire.Devices(devices=[wire.DeviceInfo(**d) for d in infos]))


async def step(rig: Rig) -> None:
    """One worker step, then the Gateway answers what it was sent."""
    await rig.worker.run_once()
    await rig.gateway_round()


async def observed(rig: Rig, *device_ids: Any) -> Any:
    """A task on these phones whose agent observed the first one."""
    rig.model.script = [[call("observe", device_id=device_ids[0])]]
    task = await rig.create_task(device_ids=list(device_ids))
    await step(rig)  # the screenshots taken as the run starts
    await step(rig)  # observe
    assert len(rig.model.requests) == 1
    return task


async def waiting_for_the_phone(rig: Rig) -> Any:
    """A task on the first phone that observed once; then the phone stopped answering."""
    task = await observed(rig, rig.tenant.device_ids[0])
    await report(rig, NOT_RESPONDING)
    await step(rig)
    return task


async def test_a_run_whose_phone_does_not_answer_waits_without_calling_the_model(
    rig: Rig,
) -> None:
    task = await waiting_for_the_phone(rig)
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.WAITING and run.wait_reason == WaitReason.DEVICE
    assert run.wait_detail == "waiting for Pixel 0 to reconnect"
    assert len(rig.model.requests) == 1  # no model call, no step spent
    assert (await rig.task(task.id)).status == TaskStatus.ACTIVE


async def test_a_run_waiting_for_its_phone_goes_on_once_the_gateway_reports_it_back(
    rig: Rig,
) -> None:
    task = await waiting_for_the_phone(rig)
    rig.model.script = [[call("observe", device_id=rig.tenant.device_ids[0])]]
    before = (await rig.event_types(task.id)).count("run.status")
    await report(rig, {})
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.RUNNABLE  # woken by the report, not by a timer
    # The task page hears of it and stops saying it waits for the phone.
    assert (await rig.event_types(task.id)).count("run.status") == before + 1
    await step(rig)
    assert len(rig.model.requests) == 2
    (_, again) = [o for o in await rig.outputs(task.id) if o["name"] == "observe"]
    assert again["status"] == "ok"  # a new screenshot to go on from


async def test_a_run_whose_phone_stays_away_past_the_limit_fails_saying_why(rig: Rig) -> None:
    task = await waiting_for_the_phone(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:  # DEVICE_WAIT has passed
        await execute(conn, "UPDATE runs SET next_wake_at = now() WHERE task_id = :t", t=task.id)
    await step(rig)
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.FAILED
    summary = str((run.result or {})["summary"])
    assert "Pixel 0" in summary and "30 minutes" in summary and "not responding" in summary
    assert (await rig.task(task.id)).status == TaskStatus.FAILED
    assert len(rig.model.requests) == 1


async def test_a_paused_task_resumed_while_its_phone_is_away_waits_for_the_phone(
    rig: Rig,
) -> None:
    task = await observed(rig, rig.tenant.device_ids[0])
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.pause(conn, task.id)
    await rig.gateway_round()  # the barrier is confirmed
    assert (await rig.task(task.id)).status == TaskStatus.PAUSED
    await report(rig, NOT_RESPONDING)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.resume(conn, task.id)
    await step(rig)
    run = await rig.root_run(task.id)
    assert run.wait_reason == WaitReason.DEVICE
    assert run.wait_detail == "waiting for Pixel 0 to reconnect"
    assert len(rig.model.requests) == 1


async def test_with_one_of_its_phones_away_a_run_goes_on_with_the_other(rig: Rig) -> None:
    first, second = rig.tenant.device_ids
    task = await rig.create_task(device_ids=[first, second])
    await report(rig, {}, NOT_RESPONDING)
    rig.model.script = [[call("observe", device_id=second)], [call("observe", device_id=first)]]
    for _ in range(3):  # the screenshot as it starts (of the first phone only), two turns
        await step(rig)
    assert len(rig.model.requests) == 2
    away, there = [o for o in await rig.outputs(task.id) if o["name"] == "observe"]
    assert away["code"] == "device_not_ready" and "not_responding" in away["detail"]
    assert there["status"] == "ok"


async def test_a_sub_agent_whose_phone_is_away_waits_for_it_and_the_main_agent_waits(
    rig: Rig,
) -> None:
    first, second = rig.tenant.device_ids
    rig.model.script = [
        [call("delegate", subtasks=[{"goal": "Open Wi-Fi settings", "device_id": str(second)}])],
    ]
    task = await rig.create_task(device_ids=[first, second])
    await report(rig, {}, NOT_RESPONDING)
    for _ in range(4):
        await step(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:
        main, child = await runs.for_task(conn, task.id)
    assert main.wait_reason == WaitReason.CHILDREN
    assert child.wait_reason == WaitReason.DEVICE
    assert child.wait_detail == "waiting for Pixel 1 to reconnect"
    assert len(rig.model.requests) == 1  # the sub-agent has not called the model
