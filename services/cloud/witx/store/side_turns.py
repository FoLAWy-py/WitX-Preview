"""The main agent's side turns while its sub-agents work."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import SideTurn
from witx.store.db import execute, fetch, fetch_one, jsonb

_TURN = """id, tenant_id, task_id, run_id, about_run_id, input_seq, text, created_at, done_at,
           error, tries"""


def _turn(row: RowMapping) -> SideTurn:
    return SideTurn(**dict(row))


async def insert(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    task_id: UUID,
    run_id: UUID,
    text: str,
    about_run_id: UUID | None = None,
    input_seq: int | None = None,
) -> None:
    await execute(
        conn,
        """INSERT INTO side_turns (id, tenant_id, task_id, run_id, about_run_id, input_seq, text)
           VALUES (:id, :tenant, :task, :run, :about, :seq, :text)""",
        id=id,
        tenant=tenant_id,
        task=task_id,
        run=run_id,
        about=about_run_id,
        seq=input_seq,
        text=text,
    )


async def get(conn: AsyncConnection, turn_id: UUID, *, lock: bool = False) -> SideTurn | None:
    row = await fetch_one(
        conn,
        f"SELECT {_TURN} FROM side_turns WHERE id = :id" + (" FOR UPDATE" if lock else ""),
        id=turn_id,
    )
    return _turn(row) if row else None


async def open_turns(conn: AsyncConnection, limit: int = 20) -> list[SideTurn]:
    """Side turns not taken yet, oldest first."""
    rows = await fetch(
        conn,
        f"SELECT {_TURN} FROM side_turns WHERE done_at IS NULL ORDER BY created_at LIMIT :n",
        n=limit,
    )
    return [_turn(r) for r in rows]


async def for_run(conn: AsyncConnection, run_id: UUID) -> list[SideTurn]:
    """The main agent's side turns, oldest first."""
    rows = await fetch(
        conn,
        f"SELECT {_TURN} FROM side_turns WHERE run_id = :r ORDER BY created_at",
        r=run_id,
    )
    return [_turn(r) for r in rows]


async def seen_inputs(conn: AsyncConnection, task_id: UUID) -> set[int]:
    """The user's messages the main agent has read in a side turn."""
    rows = await fetch(
        conn,
        """SELECT input_seq FROM side_turns WHERE task_id = :t AND input_seq IS NOT NULL
              AND done_at IS NOT NULL AND error IS NULL""",
        t=task_id,
    )
    return {int(r["input_seq"]) for r in rows}


async def withdraw(conn: AsyncConnection, task_id: UUID, seq: int, now: datetime) -> None:
    """A message withdrawn before its side turn: the side turn ends untaken."""
    await execute(
        conn,
        """UPDATE side_turns SET done_at = :now, error = 'withdrawn'
            WHERE task_id = :t AND input_seq = :s AND done_at IS NULL""",
        t=task_id,
        s=seq,
        now=now,
    )


async def finish(
    conn: AsyncConnection,
    turn_id: UUID,
    now: datetime,
    *,
    call: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    await execute(
        conn,
        """UPDATE side_turns SET done_at = :now, error = :e, tries = tries + 1,
                  call = COALESCE(CAST(:c AS jsonb), call)
            WHERE id = :id AND done_at IS NULL""",
        id=turn_id,
        now=now,
        e=error,
        c=jsonb(call) if call is not None else None,
    )


async def failed_try(conn: AsyncConnection, turn_id: UUID, call: dict[str, Any] | None) -> None:
    await execute(
        conn,
        """UPDATE side_turns SET tries = tries + 1, call = COALESCE(CAST(:c AS jsonb), call)
            WHERE id = :id AND done_at IS NULL""",
        id=turn_id,
        c=jsonb(call) if call is not None else None,
    )
