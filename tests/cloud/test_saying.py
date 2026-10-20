"""What the agent is saying, shown as the model produces it."""

import asyncio
from pathlib import Path
from typing import Any

from witx.api.routes.tasks import saying_frames
from witx.domain.model import Saying, TaskStatus
from witx.harness import loop
from witx.store import runs
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete


async def rig_with_a_task(db: Database, admin_db: Database, tmp_path: Path) -> tuple[Rig, Any]:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("observe", device_id=device)], complete]
    task = await rig.create_task()
    return rig, task


async def test_what_is_said_is_written_at_the_interval_and_never_stops_short_of_the_newest_text(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task = await rig_with_a_task(db, admin_db, tmp_path)
    async with rig.db.tenant(rig.tenant.id) as conn:
        (run,) = await runs.for_task(conn, task.id)

    async def saying() -> tuple[str | None, str | None]:
        async with rig.db.tenant(rig.tenant.id) as conn:
            current = await runs.get(conn, run.id)
        assert current is not None
        return current.saying, current.phase

    write = loop.Saying(rig.db, rig.tenant.id, run.id, run.epoch)
    await write("Looking at the screen", "talking")
    assert await saying() == ("Looking at the screen", "talking")
    await write("Looking at the screen, then", "talking")  # within the interval: held back
    await write("Looking at the screen, then Settings", "talking")
    assert await saying() == ("Looking at the screen", "talking")
    await asyncio.sleep(loop.SAYING_INTERVAL + 0.2)  # the newest text lands at its end
    assert await saying() == ("Looking at the screen, then Settings", "talking")
    await write("Done.", "planning")  # held back again
    await write.at("checking")  # a phase lands at once, with the newest words
    assert await saying() == ("Done.", "checking")
    await write.end()  # the step is over: cleared at once, nothing lands after
    assert await saying() == (None, None)
    await asyncio.sleep(loop.SAYING_INTERVAL + 0.2)
    assert await saying() == (None, None)
    stale = loop.Saying(rig.db, rig.tenant.id, run.id, run.epoch + 1)
    await stale("from a worker whose claim was taken over", "talking")
    assert await saying() == (None, None)


async def test_a_step_listens_to_the_model_as_it_speaks_and_leaves_nothing_on_the_run(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task = await rig_with_a_task(db, admin_db, tmp_path)
    await rig.settle()
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    assert all(r.on_text is not None for r in rig.model.requests)
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert all(r.saying is None for r in await runs.for_task(conn, task.id))
        assert await runs.saying_of_task(conn, task.id) == {}


async def test_the_event_stream_carries_what_the_agent_says_and_when_the_step_is_over(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    rig, task = await rig_with_a_task(db, admin_db, tmp_path)
    async with rig.db.tenant(rig.tenant.id) as conn:
        (run,) = await runs.for_task(conn, task.id)
        assert await runs.saying_of_task(conn, task.id) == {}
        await runs.set_saying(conn, run.id, run.epoch, "Settings is open.", "talking")
        now = await runs.saying_of_task(conn, task.id)
    assert now == {run.id: Saying("Settings is open.", "talking")}
    (frame,) = saying_frames({}, now)
    assert frame.startswith(b"event: agent.saying\ndata: {")  # no id: not an event
    assert b'"text":"Settings is open."' in frame and b'"phase":"talking"' in frame
    assert b"\nid:" not in frame
    assert saying_frames(now, now) == []  # unchanged: nothing to say
    checking = {run.id: Saying("Settings is open.", "checking")}
    (phase,) = saying_frames(now, checking)  # the same words, another phase
    assert b'"phase":"checking"' in phase
    (ended,) = saying_frames(checking, {})
    assert b'"text":""' in ended and b'"phase":null' in ended  # the step is over
