"""Changing the phones of a task after it started."""

from pathlib import Path

import pytest
from witx.domain.rules import Rejected
from witx.services import lifecycle
from witx.store import devices, runs, tasks
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    return rig


async def running(rig: Rig):  # type: ignore[no-untyped-def]
    first = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=first)]]
    task = await rig.create_task(device_ids=[first])
    await rig.worker.run_once()
    await rig.gateway_round()
    return task


async def set_devices(rig: Rig, task_id, device_ids):  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await lifecycle.set_devices(
            conn, task_id=task_id, user_id=rig.tenant.user_id, device_ids=device_ids
        )


async def test_a_phone_joins_a_running_task_and_the_agent_is_told(rig: Rig) -> None:
    task = await running(rig)
    first, second = rig.tenant.device_ids
    updated = await set_devices(rig, task.id, [first, second])
    assert updated.device_ids == [first, second]
    async with rig.db.tenant(rig.tenant.id) as conn:
        (run,) = await runs.for_task(conn, task.id)
        inputs = await tasks.inputs(conn, task.id)
    assert run.device_ids == [first, second]
    assert "Added" in inputs[-1]["text"] and str(second) in inputs[-1]["text"]
    assert "task.devices" in await rig.event_types(task.id)


async def test_a_phone_leaves_a_task_only_while_it_is_not_running(rig: Rig) -> None:
    task = await running(rig)
    first, second = rig.tenant.device_ids
    await set_devices(rig, task.id, [first, second])
    with pytest.raises(Rejected, match="pause_first"):
        await set_devices(rig, task.id, [second])

    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.pause(conn, task.id)
    await rig.gateway_round()  # the barrier is confirmed
    assert (await rig.task(task.id)).status.value == "paused"
    await set_devices(rig, task.id, [second])
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert await devices.lease(conn, first) is None  # given back


async def test_a_finished_task_does_not_start_over_when_a_phone_joins(rig: Rig) -> None:
    rig.model.script = [[call("complete_task", summary="done", evidence_observation_ids=[])]]
    task = await rig.create_task("What day is it?", device_ids=[])
    await rig.settle()
    assert (await rig.task(task.id)).status.value == "completed"

    first = rig.tenant.device_ids[0]
    await set_devices(rig, task.id, [first])
    await rig.settle()
    assert (await rig.task(task.id)).status.value == "completed"  # waits for you
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert len(await runs.for_task(conn, task.id)) == 1
        await lifecycle.add_input(
            conn, task_id=task.id, user_id=rig.tenant.user_id, text="Check the battery"
        )
    rig.model.script = [[call("ask_user", question="Which one?")]]
    await rig.settle()
    sent = str(rig.model.requests[-1].input)
    assert "Check the battery" in sent and str(first) in sent  # the new phone, now
