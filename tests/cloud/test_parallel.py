"""Sub-agents on several devices at once : one delegate call starts them
together, each on its own device, and answers with all their results."""

import json
from pathlib import Path
from typing import Any

import pytest
from witx.domain.model import RunStatus, TaskStatus
from witx.models import ModelRequest
from witx.store import devices, runs
from witx.store.db import Database, fetch

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    return rig


def goal_of(request: ModelRequest) -> str:
    first = request.input[0]
    return json.dumps(first.get("content"), ensure_ascii=False)


def agent(rig: Rig, delegate: dict[str, Any]) -> Any:
    """Answers by what each request is about, since sub-agents run concurrently."""
    a, b = (str(d) for d in rig.tenant.device_ids)

    def respond(request: ModelRequest) -> list[dict[str, Any]]:
        text = goal_of(request)
        outputs = [i for i in request.input if i.get("type") == "function_call_output"]
        if "Phone A" in text or "Phone B" in text:  # a sub-agent
            device = a if "Phone A" in text else b
            if not outputs:
                return [call("observe", device_id=device)]
            return [
                call(
                    "complete_task",
                    summary=f"done on {device}",
                    evidence_observation_ids=[last_screenshot(request)],
                )
            ]
        if not outputs:  # the parent, first turn
            return [delegate]
        return [call("ask_user", question="Here is the summary.")]

    return respond


async def test_one_delegate_call_runs_sub_agents_on_two_devices_at_the_same_time(
    rig: Rig,
) -> None:
    a, b = (str(d) for d in rig.tenant.device_ids)
    delegate = call(
        "delegate",
        subtasks=[
            {"goal": "Phone A: check it", "device_id": a},
            {"goal": "Phone B: check it", "device_id": b},
        ],
    )
    rig.model.script = [agent(rig, delegate)] * 12
    task = await rig.create_task()
    await rig.worker.run_once()  # the screenshots taken as the run starts
    await rig.gateway_round()
    await rig.worker.run_once()  # the parent delegates
    async with rig.db.tenant(rig.tenant.id) as conn:
        children = await runs.children(conn, (await rig.root_run(task.id)).id)
    assert [c.status for c in children] == [RunStatus.RUNNABLE, RunStatus.RUNNABLE]
    await rig.settle()

    async with rig.db.tenant(rig.tenant.id) as conn:
        first_steps = await fetch(
            conn,
            """SELECT r.id, min(s.started_at) AS first, max(s.finished_at) AS last FROM runs r
                JOIN steps s ON s.run_id = r.id
               WHERE r.parent_run_id IS NOT NULL GROUP BY r.id""",
        )
    one, two = first_steps
    assert one["first"] < two["last"] and two["first"] < one["last"]  # they overlapped
    delegate_output = next(o for o in await rig.outputs(task.id) if o["name"] == "delegate")
    assert [r["device_id"] for r in delegate_output["results"]] == [a, b]
    assert [r["status"] for r in delegate_output["results"]] == ["completed", "completed"]
    assert delegate_output["results"][0]["summary"] == f"done on {a}"
    # Both devices are back with the parent.
    root = await rig.root_run(task.id)
    async with rig.db.tenant(rig.tenant.id) as conn:
        for device in rig.tenant.device_ids:
            lease = await devices.lease(conn, device)
            assert lease is not None and lease.run_id == root.id


async def test_a_device_can_be_handed_to_only_one_sub_agent(rig: Rig) -> None:
    a = str(rig.tenant.device_ids[0])
    delegate = call(
        "delegate",
        subtasks=[
            {"goal": "Phone A: one", "device_id": a},
            {"goal": "Phone A: two", "device_id": a},
        ],
    )
    rig.model.script = [[delegate], [call("ask_user", question="?")]]
    task = await rig.create_task()
    await rig.settle()
    delegate_output = next(o for o in await rig.outputs(task.id) if o["name"] == "delegate")
    assert delegate_output["code"] == "device_handed_twice"
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert await runs.children(conn, (await rig.root_run(task.id)).id) == []
    assert (await rig.task(task.id)).status == TaskStatus.ACTIVE
