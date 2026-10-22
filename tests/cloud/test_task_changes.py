"""What a task's page fetches after each burst of events: only what changed."""

from pathlib import Path
from typing import Any

import pytest
from witx.api import views
from witx.services import lifecycle
from witx.store import tasks
from witx.store.db import Database
from witx_contracts.api import TaskChanges, TaskDetail

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision, say


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


def tap(request: Any, device: str, n: int) -> list[dict[str, Any]]:
    return [
        call(
            "tap",
            device_id=device,
            observation_id=last_screenshot(request),
            x=540,
            y=200 + n * 100,
            target=f"row {n}",
            expected_effect="it opens",
            effects=[],
            notes=f"row {n} was on screen",
        )
    ]


async def detail(rig: Rig, task_id: Any, since: int | None = None) -> TaskDetail:
    async with rig.db.tenant(rig.tenant.id) as conn:
        task = await tasks.get(conn, task_id)
        assert task is not None
        if since is None:
            return await views.task_detail(conn, task)
        return await views.task_changes(conn, task, since)


async def every_state(rig: Rig, task_id: Any) -> list[TaskDetail]:
    """The task as its page would see it after each worker step and Gateway exchange."""
    seen = [await detail(rig, task_id)]
    for _ in range(30):
        moved = await rig.worker.run_once()
        seen.append(await detail(rig, task_id))
        moved += await rig.gateway_round()
        seen.append(await detail(rig, task_id))
        if not moved:
            break
    return seen


async def test_the_changes_since_any_moment_hold_every_record_that_changed_since(
    rig: Rig,
) -> None:
    device = str(rig.tenant.device_ids[0])
    rig.model.script = [
        [say("Looking."), call("observe", device_id=device)],
        *[lambda r, n=n: tap(r, device, n) for n in range(3)],
        lambda r: [
            call("complete_task", summary="done", evidence_observation_ids=[last_screenshot(r)])
        ],
    ]
    task = await rig.create_task()
    states = await every_state(rig, task.id)
    now = states[-1]
    assert len(now.actions) == 3 and len(now.notes) == 3

    for then in states:
        changes = await detail(rig, task.id, since=then.last_event_seq)
        assert isinstance(changes, TaskChanges)
        for kind in ("actions", "observations", "runs"):
            before = {r.id: r for r in getattr(then, kind)}
            sent = {r.id: r for r in getattr(changes, kind)}
            for record in getattr(now, kind):
                if before.get(record.id) != record:
                    assert sent.get(record.id) == record, (kind, then.last_event_seq)
        # A run's notes come whole with the run; the other runs' notes did not change.
        sent_runs = {r.id for r in changes.runs}
        for run in now.runs:
            mine = [n for n in now.notes if n.run_id == run.id]
            if run.id in sent_runs:
                assert [n for n in changes.notes if n.run_id == run.id] == mine
            else:
                assert [n for n in then.notes if n.run_id == run.id] == mine
        assert all(n.run_id in sent_runs for n in changes.notes)


async def test_the_changes_after_the_last_event_hold_nothing_that_is_finished(rig: Rig) -> None:
    device = str(rig.tenant.device_ids[0])
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, 0),
        lambda r: [
            call("complete_task", summary="done", evidence_observation_ids=[last_screenshot(r)])
        ],
    ]
    task = await rig.create_task()
    *_, now = await every_state(rig, task.id)

    changes = await detail(rig, task.id, since=now.last_event_seq)

    assert (changes.actions, changes.observations, changes.runs, changes.notes) == ([], [], [], [])
    assert changes.status == now.status and changes.inputs == now.inputs  # the rest is whole


async def test_what_the_page_holds_open_comes_back_finished_even_with_no_event_naming_it(
    rig: Rig,
) -> None:
    device = str(rig.tenant.device_ids[0])
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: [
            call(
                "tap",
                device_id=device,
                observation_id=last_screenshot(r),
                x=540,
                y=1200,
                target="Delete",
                expected_effect="the note is deleted",
                effects=["delete_data"],
            )
        ],
    ]
    task = await rig.create_task("Delete the note")
    await rig.settle()
    page = await detail(rig, task.id)
    (waiting,) = page.actions
    assert waiting.status == "awaiting_approval"
    held_open = {waiting.id, *(r.id for r in page.runs if r.status not in ("completed",))}

    # Stopping cancels the input and the run without an event that names either.
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.stop(conn, task.id)
        stopped = await tasks.get(conn, task.id)
        assert stopped is not None
        changes = await views.task_changes(conn, stopped, page.last_event_seq, held_open)

    assert [(a.id, a.status) for a in changes.actions] == [(waiting.id, "cancelled")]
    assert {r.id for r in changes.runs} == held_open - {waiting.id}
    assert all(r.status == "cancelled" for r in changes.runs)
