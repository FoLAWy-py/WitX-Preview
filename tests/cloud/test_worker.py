"""Claims on runs: a dead worker's runs go to another worker once its lease runs out."""

import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from witx.services import schedules
from witx.store import runs
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import device_info, provision


async def test_a_dead_workers_run_is_claimed_again_and_its_late_writes_are_fenced(
    db: Database, admin_db: Database, tmp_path: Path
) -> None:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    task = await rig.create_task()
    async with db.system() as conn:
        ((run_id, _, first_epoch),) = await runs.claim(conn, "worker-a", 1, 10)
    async with db.system() as conn:
        assert await runs.claim(conn, "worker-b", 1, 10) == []  # still held by worker-a
    await asyncio.sleep(1.2)  # worker-a died: no heartbeat
    async with db.system() as conn:
        ((again, _, second_epoch),) = await runs.claim(conn, "worker-b", 60, 10)
    assert again == run_id == (await rig.root_run(task.id)).id
    assert second_epoch == first_epoch + 1
    async with db.tenant(tenant.id) as conn:
        assert not await runs.heartbeat(conn, run_id, first_epoch, 60)  # worker-a is fenced
        assert await runs.heartbeat(conn, run_id, second_epoch, 60)


async def test_a_database_outage_does_not_stop_the_worker(db: Database) -> None:
    from witx.worker import Worker

    worker = Worker(db, None, poll_seconds=0.01)  # type: ignore[arg-type]
    waits: list[float] = []
    real_wait_for = asyncio.wait_for

    async def short(awaitable, timeout):  # type: ignore[no-untyped-def]
        waits.append(timeout)
        return await real_wait_for(awaitable, 0.01)

    attempts: list[int] = []
    stop = asyncio.Event()

    async def claim(limit: int) -> list[tuple[UUID, UUID, int]]:
        attempts.append(limit)
        if len(attempts) < 3:
            raise OSError("connection refused")  # the database is down
        stop.set()
        return []

    worker.claim = claim  # type: ignore[method-assign]
    asyncio.wait_for = short  # type: ignore[assignment]
    try:
        await real_wait_for(worker.run_forever(stop), 30)
    finally:
        asyncio.wait_for = real_wait_for  # type: ignore[assignment]
    assert len(attempts) == 3  # it kept trying until the database answered again
    assert waits[:2] == [1.0, 2.0]  # backing off meanwhile


async def test_the_schedule_loop_looks_again_when_the_soonest_schedule_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from witx import worker

    stop = asyncio.Event()
    waits: list[float] = []

    async def run_due(db: Database, now: datetime) -> schedules.Pass:
        return schedules.Pass(started=0, soonest=now + timedelta(seconds=3))

    async def wait_for(awaitable: Coroutine[object, object, object], timeout: float) -> None:
        awaitable.close()
        waits.append(timeout)
        stop.set()
        raise TimeoutError

    monkeypatch.setattr(worker.schedules, "run_due", run_due)
    monkeypatch.setattr(worker.asyncio, "wait_for", wait_for)
    await worker.schedule_forever(None, stop)  # type: ignore[arg-type]
    # A schedule three seconds away: the next look is then, not 15 s later.
    assert waits == [pytest.approx(3.0, abs=0.5)]


async def test_a_database_outage_during_a_step_does_not_end_it(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from witx.worker import Worker

    finished = asyncio.Event()

    class SlowStep:
        async def step(self, run_id: UUID, tenant_id: UUID, epoch: int) -> None:
            await asyncio.sleep(0.2)  # long enough for heartbeats
            finished.set()

    async def unavailable(*args: object, **kwargs: object) -> bool:
        raise OSError("connection refused")

    monkeypatch.setattr(runs, "heartbeat", unavailable)
    worker = Worker(db, SlowStep(), lease_seconds=0.15)  # type: ignore[arg-type]
    await worker.execute(uuid4(), uuid4(), 1)  # does not raise
    assert finished.is_set()
