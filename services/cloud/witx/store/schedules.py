"""Scheduled tasks (see services.schedules)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import PermissionMode, Repeat, Schedule
from witx.store.db import execute, fetch, fetch_one

_COLUMNS = """id, tenant_id, created_by, goal, constraints, device_ids, permission_mode, model,
              repeat, time_of_day, timezone, weekdays, month_day, run_on, enabled,
              next_run_at, last_run_at, last_task_id, last_outcome, created_at,
              reasoning_effort"""


def _schedule(row: RowMapping) -> Schedule:
    return Schedule(
        id=row["id"],
        tenant_id=row["tenant_id"],
        created_by=row["created_by"],
        goal=row["goal"],
        constraints=list(row["constraints"]),
        device_ids=list(row["device_ids"]),
        permission_mode=PermissionMode(row["permission_mode"]),
        model=row["model"],
        repeat=Repeat(row["repeat"]),
        time_of_day=row["time_of_day"],
        timezone=row["timezone"],
        weekdays=list(row["weekdays"]),
        month_day=row["month_day"],
        run_on=row["run_on"],
        enabled=row["enabled"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        last_task_id=row["last_task_id"],
        last_outcome=row["last_outcome"],
        created_at=row["created_at"],
        reasoning_effort=row["reasoning_effort"],
    )


async def insert(conn: AsyncConnection, schedule: Schedule) -> Schedule:
    row = await fetch_one(
        conn,
        f"""INSERT INTO schedules (id, tenant_id, created_by, goal, constraints, device_ids,
                   permission_mode, model, repeat, time_of_day, timezone, weekdays, month_day,
                   run_on, enabled, next_run_at, reasoning_effort, last_outcome)
            VALUES (:id, :tenant_id, :created_by, :goal, :constraints, :device_ids,
                    :permission_mode, :model, :repeat, :time_of_day, :timezone, :weekdays,
                    :month_day, :run_on, :enabled, :next_run_at, :reasoning_effort,
                    :last_outcome)
            RETURNING {_COLUMNS}""",
        id=schedule.id,
        tenant_id=schedule.tenant_id,
        created_by=schedule.created_by,
        goal=schedule.goal,
        constraints=schedule.constraints,
        device_ids=schedule.device_ids,
        permission_mode=schedule.permission_mode.value,
        model=schedule.model,
        repeat=schedule.repeat.value,
        time_of_day=schedule.time_of_day,
        timezone=schedule.timezone,
        weekdays=schedule.weekdays,
        month_day=schedule.month_day,
        run_on=schedule.run_on,
        enabled=schedule.enabled,
        next_run_at=schedule.next_run_at,
        reasoning_effort=schedule.reasoning_effort,
        last_outcome=schedule.last_outcome,
    )
    assert row is not None
    return _schedule(row)


async def get(conn: AsyncConnection, schedule_id: UUID, *, lock: bool = False) -> Schedule | None:
    row = await fetch_one(
        conn,
        f"SELECT {_COLUMNS} FROM schedules WHERE id = :id" + (" FOR UPDATE" if lock else ""),
        id=schedule_id,
    )
    return _schedule(row) if row else None


async def list_all(conn: AsyncConnection) -> list[Schedule]:
    rows = await fetch(
        conn,
        f"""SELECT {_COLUMNS} FROM schedules
             ORDER BY enabled DESC, next_run_at NULLS LAST, created_at DESC""",
    )
    return [_schedule(r) for r in rows]


async def due(conn: AsyncConnection, now: datetime, limit: int = 20) -> list[Schedule]:
    """Enabled schedules whose moment has come, locked for whoever runs them; another
    worker skips them."""
    rows = await fetch(
        conn,
        f"""SELECT {_COLUMNS} FROM schedules
             WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= :now
             ORDER BY next_run_at LIMIT :limit FOR UPDATE SKIP LOCKED""",
        now=now,
        limit=limit,
    )
    return [_schedule(r) for r in rows]


async def soonest(conn: AsyncConnection, after: datetime) -> datetime | None:
    """When the next enabled schedule is due, strictly after ``after``."""
    row = await fetch_one(
        conn,
        """SELECT min(next_run_at) AS soonest FROM schedules
            WHERE enabled AND next_run_at > :after""",
        after=after,
    )
    return row["soonest"] if row else None


async def update(conn: AsyncConnection, schedule_id: UUID, **fields: Any) -> None:
    allowed = {
        "enabled",
        "next_run_at",
        "last_run_at",
        "last_task_id",
        "last_outcome",
    }
    assert set(fields) <= allowed, set(fields) - allowed
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await execute(conn, f"UPDATE schedules SET {sets} WHERE id = :id", id=schedule_id, **fields)


async def redefine(conn: AsyncConnection, schedule: Schedule) -> Schedule:
    """Writes what a schedule does and when, and its next moment."""
    row = await fetch_one(
        conn,
        f"""UPDATE schedules SET goal = :goal, constraints = :constraints,
                   device_ids = :device_ids, permission_mode = :permission_mode,
                   model = :model, repeat = :repeat, time_of_day = :time_of_day,
                   timezone = :timezone, weekdays = :weekdays, month_day = :month_day,
                   run_on = :run_on, reasoning_effort = :reasoning_effort,
                   next_run_at = :next_run_at
            WHERE id = :id
            RETURNING {_COLUMNS}""",
        id=schedule.id,
        goal=schedule.goal,
        constraints=schedule.constraints,
        device_ids=schedule.device_ids,
        permission_mode=schedule.permission_mode.value,
        model=schedule.model,
        repeat=schedule.repeat.value,
        time_of_day=schedule.time_of_day,
        timezone=schedule.timezone,
        weekdays=schedule.weekdays,
        month_day=schedule.month_day,
        run_on=schedule.run_on,
        reasoning_effort=schedule.reasoning_effort,
        next_run_at=schedule.next_run_at,
    )
    assert row is not None
    return _schedule(row)


async def delete(conn: AsyncConnection, schedule_id: UUID) -> None:
    await execute(conn, "DELETE FROM schedules WHERE id = :id", id=schedule_id)
