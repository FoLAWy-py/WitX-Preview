"""Scheduled tasks start by themselves, as the person who made them."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from witx.domain.model import PermissionMode, Repeat
from witx.domain.rules import Rejected
from witx.services import schedules
from witx.services.identity import Actor
from witx.store import schedules as store
from witx.store import tasks
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision

MONDAY_8AM = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)  # 08:00 in Shanghai


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


def owner(rig: Rig) -> Actor:
    return Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")


async def daily(rig: Rig, **kw):  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await schedules.create(
            conn,
            owner(rig),
            goal="Check the battery",
            constraints=[],
            device_ids=rig.tenant.device_ids,
            permission_mode=PermissionMode.AUTO,
            model="test-model",
            repeat=kw.pop("repeat", Repeat.DAILY),
            time_of_day=time(8, 0),
            timezone="Asia/Shanghai",
            weekdays=[],
            month_day=None,
            run_on=kw.pop("run_on", None),
            now=kw.pop("now", MONDAY_8AM - timedelta(hours=1)),
        )


async def current(rig: Rig, schedule_id):  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await store.get(conn, schedule_id)


async def test_a_due_schedule_starts_its_task_as_its_maker_and_waits_for_the_next_day(
    rig: Rig,
) -> None:
    schedule = await daily(rig)
    assert schedule.next_run_at == MONDAY_8AM

    assert (await schedules.run_due(rig.db, MONDAY_8AM + timedelta(seconds=10))).started == 1
    after = await current(rig, schedule.id)
    assert after.last_outcome == "started" and after.last_task_id is not None
    assert after.next_run_at == MONDAY_8AM + timedelta(days=1)
    async with rig.db.tenant(rig.tenant.id) as conn:
        task = await tasks.get(conn, after.last_task_id)
    assert task is not None and task.request == "Check the battery"
    assert task.timezone == "Asia/Shanghai"  # the schedule's, for "tomorrow"
    # Not due again the same day.
    assert (await schedules.run_due(rig.db, MONDAY_8AM + timedelta(minutes=1))).started == 0


async def test_a_busy_phone_is_waited_for_then_the_run_is_skipped(rig: Rig) -> None:
    rig.model.script = [[call("observe", device_id=rig.tenant.device_ids[0])]]
    await rig.create_task("Something else")
    await rig.worker.run_once()  # that task now holds the phone
    schedule = await daily(rig)

    assert (await schedules.run_due(rig.db, MONDAY_8AM + timedelta(minutes=5))).started == 0
    waiting = await current(rig, schedule.id)
    assert waiting.last_outcome == "waiting:device_busy"
    assert waiting.next_run_at == MONDAY_8AM  # still this morning's run

    assert (await schedules.run_due(rig.db, MONDAY_8AM + timedelta(minutes=31))).started == 0
    skipped = await current(rig, schedule.id)
    assert skipped.last_outcome == "skipped:device_busy"
    assert skipped.next_run_at == MONDAY_8AM + timedelta(days=1)


async def test_a_pass_says_when_the_next_schedule_is_due(rig: Rig) -> None:
    await daily(rig)
    soon = await daily(rig, repeat=Repeat.ONCE, run_on=date(2026, 10, 12))
    assert soon.next_run_at == MONDAY_8AM
    # Three seconds before both are due: the soonest is what the worker waits for.
    done = await schedules.run_due(rig.db, MONDAY_8AM - timedelta(seconds=3))
    assert done.started == 0 and done.soonest == MONDAY_8AM
    # After they ran, the daily one's next morning is next; the one-time one is done.
    done = await schedules.run_due(rig.db, MONDAY_8AM)
    assert done.soonest == MONDAY_8AM + timedelta(days=1)


async def test_a_one_time_schedule_runs_once(rig: Rig) -> None:
    schedule = await daily(rig, repeat=Repeat.ONCE, run_on=date(2026, 10, 12))
    assert (await schedules.run_due(rig.db, MONDAY_8AM)).started == 1
    assert (await current(rig, schedule.id)).next_run_at is None


async def test_a_resumed_schedule_counts_from_now_not_from_missed_runs(rig: Rig) -> None:
    schedule = await daily(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:
        paused = await schedules.set_enabled(conn, schedule.id, False, MONDAY_8AM)
        assert paused.next_run_at is None
        resumed = await schedules.set_enabled(
            conn, schedule.id, True, MONDAY_8AM + timedelta(days=3, hours=2)
        )
    assert resumed.next_run_at == MONDAY_8AM + timedelta(days=4)


async def edited(rig: Rig, schedule_id, now: datetime, **kw):  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        return await schedules.edit(
            conn,
            schedule_id,
            goal=kw.pop("goal", "Check the battery and the storage"),
            constraints=[],
            device_ids=rig.tenant.device_ids,
            permission_mode=PermissionMode.AUTO,
            model="test-model",
            repeat=kw.pop("repeat", Repeat.WEEKLY),
            time_of_day=time(21, 30),
            timezone="Asia/Shanghai",
            weekdays=kw.pop("weekdays", [2]),  # Wednesday
            month_day=None,
            run_on=None,
            reasoning_effort="medium",
            now=now,
        )


async def test_an_edited_schedule_runs_at_its_new_time_and_keeps_what_it_last_did(
    rig: Rig,
) -> None:
    schedule = await daily(rig)
    rig.model.script = [[call("complete_task", summary="ok", evidence_observation_ids=[])]]
    assert (await schedules.run_due(rig.db, MONDAY_8AM)).started == 1
    ran = await current(rig, schedule.id)
    changed = await edited(rig, schedule.id, MONDAY_8AM + timedelta(hours=1))
    assert changed.goal == "Check the battery and the storage"
    # Wednesday 21:30 in Shanghai, the first such moment from now.
    assert changed.next_run_at == datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    assert (changed.last_task_id, changed.last_run_at) == (ran.last_task_id, ran.last_run_at)


async def test_a_paused_schedule_stays_paused_when_edited(rig: Rig) -> None:
    schedule = await daily(rig)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await schedules.set_enabled(conn, schedule.id, False, MONDAY_8AM)
    changed = await edited(rig, schedule.id, MONDAY_8AM)
    assert (changed.enabled, changed.next_run_at) == (False, None)


async def test_an_edit_is_checked_as_when_the_schedule_was_made(rig: Rig) -> None:
    schedule = await daily(rig)
    with pytest.raises(Rejected) as refused:
        await edited(rig, schedule.id, MONDAY_8AM, weekdays=[])  # weekly, but no day
    assert refused.value.code == "invalid_schedule"
    assert (await current(rig, schedule.id)).goal == "Check the battery"
