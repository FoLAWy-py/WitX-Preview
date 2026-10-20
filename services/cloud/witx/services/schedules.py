"""Scheduled tasks: a task that starts by itself, once or repeatedly, at a local time. At its
    moment the worker starts the task as the person who made the
schedule, with the same checks as starting it by hand. A phone that is busy or not ready
is waited for up to SCHEDULE_GRACE; after that the run is skipped and the reason kept."""

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import logs

from witx.domain import rules
from witx.domain.model import PermissionMode, Repeat, Schedule, Task
from witx.domain.rules import Rejected
from witx.services import lifecycle
from witx.services.identity import Actor
from witx.store import devices, identity, tasks
from witx.store import schedules as store
from witx.store.db import Database

log = logging.getLogger(__name__)

# Reasons worth waiting out within the grace period.
_WAIT_FOR = {"device_busy", "device_not_ready", "device_offline"}


def _next(schedule: Schedule, after: datetime) -> datetime | None:
    return rules.next_run(
        repeat=schedule.repeat,
        time_of_day=schedule.time_of_day,
        timezone=schedule.timezone,
        weekdays=schedule.weekdays,
        month_day=schedule.month_day,
        run_on=schedule.run_on,
        after=after,
    )


async def create(
    conn: AsyncConnection,
    actor: Actor,
    *,
    goal: str,
    constraints: list[str],
    device_ids: list[UUID],
    permission_mode: PermissionMode,
    model: str,
    repeat: Repeat,
    time_of_day: time,
    timezone: str,
    weekdays: list[int],
    month_day: int | None,
    run_on: date | None,
    now: datetime,
    reasoning_effort: str = "medium",
) -> Schedule:
    draft = await _checked(
        conn,
        Schedule(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            created_by=actor.user_id,
            goal=goal,
            constraints=constraints,
            device_ids=device_ids,
            permission_mode=permission_mode,
            model=model,
            repeat=repeat,
            time_of_day=time_of_day,
            timezone=timezone,
            weekdays=weekdays,
            month_day=month_day,
            run_on=run_on,
            enabled=True,
            next_run_at=None,
            last_run_at=None,
            last_task_id=None,
            last_outcome=None,
            created_at=now,
            reasoning_effort=reasoning_effort,
        ),
    )
    first = _next(draft, now)
    if first is None:
        raise Rejected("invalid_schedule", "that moment has already passed")
    return await store.insert(conn, replace(draft, next_run_at=first))


async def _checked(conn: AsyncConnection, schedule: Schedule) -> Schedule:
    """A schedule's definition as created or edited: checked, and only the fields its
    kind of repeat uses kept."""
    # An effort at all; how low the schedule's model goes is checked where it is chosen.
    rules.check_reasoning_effort(schedule.reasoning_effort, rules.REASONING_EFFORTS[0])
    rules.check_schedule(
        repeat=schedule.repeat,
        timezone=schedule.timezone,
        weekdays=schedule.weekdays,
        month_day=schedule.month_day,
        run_on=schedule.run_on,
    )
    await lifecycle.check_mode_allowed(conn, schedule.tenant_id, schedule.permission_mode)
    repeat = schedule.repeat
    return replace(
        schedule,
        device_ids=list(dict.fromkeys(schedule.device_ids)),
        weekdays=sorted(set(schedule.weekdays)) if repeat == Repeat.WEEKLY else [],
        month_day=schedule.month_day if repeat == Repeat.MONTHLY else None,
        run_on=schedule.run_on if repeat == Repeat.ONCE else None,
    )


async def edit(
    conn: AsyncConnection,
    schedule_id: UUID,
    *,
    goal: str,
    constraints: list[str],
    device_ids: list[UUID],
    permission_mode: PermissionMode,
    model: str,
    repeat: Repeat,
    time_of_day: time,
    timezone: str,
    weekdays: list[int],
    month_day: int | None,
    run_on: date | None,
    reasoning_effort: str,
    now: datetime,
) -> Schedule:
    """A schedule given a new definition, checked as when it was made. It stays on or
    off (a proposal stays a proposal) and keeps who made it and what it last did; an
    active one counts its next moment from now."""
    current = await store.get(conn, schedule_id, lock=True)
    if current is None:
        raise Rejected("not_found", "schedule not found")
    edited = await _checked(
        conn,
        replace(
            current,
            goal=goal,
            constraints=constraints,
            device_ids=device_ids,
            permission_mode=permission_mode,
            model=model,
            repeat=repeat,
            time_of_day=time_of_day,
            timezone=timezone,
            weekdays=weekdays,
            month_day=month_day,
            run_on=run_on,
            reasoning_effort=reasoning_effort,
        ),
    )
    first = _next(edited, now) if current.enabled else None
    if current.enabled and first is None:
        raise Rejected("invalid_schedule", "that moment has already passed")
    return await store.redefine(conn, replace(edited, next_run_at=first))


async def set_enabled(
    conn: AsyncConnection,
    schedule_id: UUID,
    enabled: bool,
    now: datetime,
) -> Schedule:
    """Pausing keeps the schedule; resuming counts from now, not from missed moments."""
    schedule = await store.get(conn, schedule_id, lock=True)
    if schedule is None:
        raise Rejected("not_found", "schedule not found")
    first = _next(schedule, now) if enabled else None
    if enabled and first is None:
        raise Rejected("invalid_schedule", "that moment has already passed")
    await store.update(conn, schedule_id, enabled=enabled, next_run_at=first)
    updated = await store.get(conn, schedule_id)
    assert updated is not None
    return updated


async def propose(
    conn: AsyncConnection,
    task: Task,
    *,
    goal: str,
    repeat: Repeat,
    time_of_day: time,
    weekdays: list[int],
    month_day: int | None,
    run_on: date | None,
    now: datetime,
) -> Schedule:
    """A schedule the agent proposes from the user's words: off until the user turns it
    on, with the task's phones, settings and time zone."""
    goal = goal.strip()[:4000]
    if not goal:
        raise Rejected("invalid_schedule", "a schedule needs a goal")
    rules.check_schedule(
        repeat=repeat, timezone=task.timezone, weekdays=weekdays, month_day=month_day, run_on=run_on
    )
    maker = await tasks.creator(conn, task.id)
    return await store.insert(
        conn,
        Schedule(
            id=uuid4(),
            tenant_id=task.tenant_id,
            created_by=maker,
            goal=goal,
            constraints=[],
            device_ids=list(task.device_ids),
            permission_mode=task.permission_mode,
            model=task.model,
            repeat=repeat,
            time_of_day=time_of_day,
            timezone=task.timezone,
            weekdays=sorted(set(weekdays)) if repeat == Repeat.WEEKLY else [],
            month_day=month_day if repeat == Repeat.MONTHLY else None,
            run_on=run_on if repeat == Repeat.ONCE else None,
            enabled=False,
            next_run_at=None,
            last_run_at=None,
            last_task_id=None,
            last_outcome="proposed",
            created_at=now,
            reasoning_effort=task.reasoning_effort,
        ),
    )


@dataclass(frozen=True)
class Pass:
    """One look at the schedules: how many tasks started, and when the next schedule
    after this moment is due (None if none is), for the worker to wake up then."""

    started: int
    soonest: datetime | None


async def run_due(db: Database, now: datetime) -> Pass:
    """Starts the tasks of every schedule whose moment has come."""
    async with db.system() as conn:
        tenants = [row["id"] for row in await identity.tenants(conn)]
    started = 0
    soonest: datetime | None = None
    for tenant_id in tenants:
        with logs.log_context(tenant_id=tenant_id):
            async with db.tenant(tenant_id) as conn:
                for schedule in await store.due(conn, now):
                    started += await _run(conn, schedule, now)
                # Waiting schedules are due already: they are tried again at the
                # worker's regular look, not in a busy loop.
                upcoming = await store.soonest(conn, now)
        if upcoming is not None and (soonest is None or upcoming < soonest):
            soonest = upcoming
    return Pass(started, soonest)


async def _run(conn: AsyncConnection, schedule: Schedule, now: datetime) -> int:
    assert schedule.next_run_at is not None
    late = now - schedule.next_run_at
    try:
        held = await devices.leases(conn, schedule.device_ids)
        if not all(rules.held_by_nobody(lease, now) for lease in held):
            raise Rejected("device_busy", "a phone works on another task")
        task = await lifecycle.create_task(
            conn,
            tenant_id=schedule.tenant_id,
            user_id=schedule.created_by,
            request=schedule.goal,
            constraints=schedule.constraints,
            device_ids=schedule.device_ids,
            model=schedule.model,
            permission_mode=schedule.permission_mode,
            reasoning_effort=schedule.reasoning_effort,
            timezone=schedule.timezone,
        )
    except Rejected as error:
        if error.code in _WAIT_FOR and late <= rules.SCHEDULE_GRACE:
            await store.update(conn, schedule.id, last_outcome=f"waiting:{error.code}")
            return 0
        log.info("scheduled run skipped: %s", error.code)
        await store.update(
            conn,
            schedule.id,
            last_run_at=now,
            last_outcome=f"skipped:{error.code}",
            next_run_at=_next(schedule, now),
        )
        return 0
    await store.update(
        conn,
        schedule.id,
        last_run_at=now,
        last_task_id=task.id,
        last_outcome="started",
        next_run_at=_next(schedule, now),
    )
    return 1
