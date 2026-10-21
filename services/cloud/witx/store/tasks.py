"""Tasks, user inputs and the per-task event stream."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import PermissionMode, Task, TaskGroup, TaskStatus
from witx.store.db import execute, fetch, fetch_one, jsonb

_TASK = """id, tenant_id, goal, constraints, status, revision, control_revision, model,
           device_ids, result, created_at, updated_at, permission_mode, group_id,
           reasoning_effort, title, timezone, request, goal_paused, plan_mode"""


def _task(row: RowMapping) -> Task:
    return Task(
        id=row["id"],
        tenant_id=row["tenant_id"],
        goal=row["goal"],
        constraints=list(row["constraints"]),
        status=TaskStatus(row["status"]),
        revision=row["revision"],
        control_revision=row["control_revision"],
        model=row["model"],
        device_ids=list(row["device_ids"]),
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        permission_mode=PermissionMode(row["permission_mode"]),
        group_id=row["group_id"],
        reasoning_effort=row["reasoning_effort"],
        title=row["title"],
        timezone=row["timezone"],
        request=row["request"],
        goal_paused=row["goal_paused"],
        plan_mode=row["plan_mode"],
    )


async def insert(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    created_by: UUID,
    goal: str,
    constraints: list[str],
    model: str,
    device_ids: list[UUID],
    permission_mode: PermissionMode,
    reasoning_effort: str = "medium",
    timezone: str = "UTC",
    request: str = "",
    plan_mode: bool = False,
    group_id: UUID | None = None,
) -> Task:
    row = await fetch_one(
        conn,
        f"""INSERT INTO tasks (id, tenant_id, created_by, goal, constraints, status, model,
                               device_ids, permission_mode, reasoning_effort, timezone, request,
                               plan_mode, group_id)
            VALUES (:id, :tenant_id, :created_by, :goal, :constraints, 'active', :model,
                    :device_ids, :permission_mode, :reasoning_effort, :timezone, :request,
                    :plan_mode, :group_id)
            RETURNING {_TASK}""",
        id=id,
        tenant_id=tenant_id,
        created_by=created_by,
        goal=goal,
        constraints=constraints,
        model=model,
        device_ids=device_ids,
        permission_mode=permission_mode.value,
        reasoning_effort=reasoning_effort,
        timezone=timezone,
        request=request,
        plan_mode=plan_mode,
        group_id=group_id,
    )
    assert row is not None
    return _task(row)


async def get(conn: AsyncConnection, task_id: UUID, *, lock: bool = False) -> Task | None:
    row = await fetch_one(
        conn,
        f"SELECT {_TASK} FROM tasks WHERE id = :id AND deleted_at IS NULL"
        + (" FOR UPDATE" if lock else ""),
        id=task_id,
    )
    return _task(row) if row else None


_NEEDS_ATTENTION = """(EXISTS (SELECT 1 FROM actions a WHERE a.task_id = tasks.id
                            AND (a.status = 'awaiting_approval' OR a.effect = 'unknown'))
                     OR EXISTS (SELECT 1 FROM runs r WHERE r.task_id = tasks.id
                            AND r.status = 'waiting' AND r.wait_reason = 'user_input'))"""

UNGROUPED = "none"


async def list_with_attention(
    conn: AsyncConnection,
    limit: int = 100,
    *,
    group: UUID | str | None = None,
    attention_only: bool = False,
    task_id: UUID | None = None,
    search: str | None = None,
    pins: tuple[UUID, str] | None = None,
) -> list[tuple[Task, bool]]:
    """Recent tasks, each flagged if it waits for the user (approval, answer, unknown
    effect). ``group`` keeps one group's tasks, or the ungrouped ones (UNGROUPED);
    ``task_id`` just that task; ``search`` those whose goal contains the text;
    ``pins``, a person and PINNED_ONLY or PINNED_WITHOUT, only the tasks they pinned
    (latest pinned first) or none of them."""
    where = ["deleted_at IS NULL"]
    params: dict[str, Any] = {"limit": limit}
    order = "updated_at DESC"
    if pins is not None:
        params["pinner"], which = pins
        pinned = (
            "EXISTS (SELECT 1 FROM task_pins p WHERE p.task_id = tasks.id AND p.user_id = :pinner)"
        )
        where.append(pinned if which == PINNED_ONLY else f"NOT {pinned}")
        if which == PINNED_ONLY:
            order = (
                "(SELECT pinned_at FROM task_pins p WHERE p.task_id = tasks.id"
                " AND p.user_id = :pinner) DESC"
            )
    if search:
        where.append(
            "(request ILIKE :search ESCAPE '\\' OR goal ILIKE :search ESCAPE '\\'"
            " OR title ILIKE :search ESCAPE '\\')"
        )
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["search"] = f"%{escaped}%"
    if task_id is not None:
        where.append("id = :task_id")
        params["task_id"] = task_id
    if group == UNGROUPED:
        where.append("group_id IS NULL")
    elif group is not None:
        where.append("group_id = :group")
        params["group"] = group
    if attention_only:
        where.append(_NEEDS_ATTENTION)
    rows = await fetch(
        conn,
        f"""SELECT {_TASK}, {_NEEDS_ATTENTION} AS needs_attention
              FROM tasks WHERE {" AND ".join(where)}
             ORDER BY {order} LIMIT :limit""",
        **params,
    )
    return [(_task(r), bool(r["needs_attention"])) for r in rows]


PINNED_ONLY = "only"
PINNED_WITHOUT = "without"


async def pinned(conn: AsyncConnection, user_id: UUID, task_ids: Sequence[UUID]) -> set[UUID]:
    """Which of these tasks this person pinned."""
    if not task_ids:
        return set()
    rows = await fetch(
        conn,
        "SELECT task_id FROM task_pins WHERE user_id = :u AND task_id = ANY(:ids)",
        u=user_id,
        ids=list(task_ids),
    )
    return {r["task_id"] for r in rows}


async def pin(conn: AsyncConnection, tenant_id: UUID, user_id: UUID, task_id: UUID) -> bool:
    """Pins a task for this person; False if there is no such task. Pinning again keeps
    its first place."""
    found = await fetch_one(
        conn, "SELECT 1 FROM tasks WHERE id = :t AND deleted_at IS NULL", t=task_id
    )
    if found is None:
        return False
    await execute(
        conn,
        """INSERT INTO task_pins (tenant_id, user_id, task_id) VALUES (:t, :u, :task)
           ON CONFLICT (user_id, task_id) DO NOTHING""",
        t=tenant_id,
        u=user_id,
        task=task_id,
    )
    return True


async def unpin(conn: AsyncConnection, user_id: UUID, task_id: UUID) -> None:
    await execute(
        conn, "DELETE FROM task_pins WHERE user_id = :u AND task_id = :t", u=user_id, t=task_id
    )


async def goals(conn: AsyncConnection, task_ids: Sequence[UUID]) -> dict[UUID, str]:
    """The goal of each of these tasks."""
    if not task_ids:
        return {}
    rows = await fetch(
        conn,
        "SELECT id, coalesce(title, nullif(request, ''), goal) AS goal FROM tasks"
        " WHERE id = ANY(:ids)",
        ids=list(task_ids),
    )
    return {r["id"]: r["goal"] for r in rows}


async def set_group(conn: AsyncConnection, task_id: UUID, group_id: UUID | None) -> bool:
    """Files a task in a group (or none). False if there is no such task. Moving a task
    is not a change to the task, so its place in the recent list stays."""
    rows = await fetch(
        conn,
        "UPDATE tasks SET group_id = :g WHERE id = :id AND deleted_at IS NULL RETURNING id",
        id=task_id,
        g=group_id,
    )
    return bool(rows)


def _group(row: RowMapping) -> TaskGroup:
    return TaskGroup(id=row["id"], name=row["name"], created_at=row["created_at"])


async def list_groups(conn: AsyncConnection) -> list[TaskGroup]:
    rows = await fetch(conn, "SELECT id, name, created_at FROM task_groups ORDER BY name, id")
    return [_group(r) for r in rows]


async def get_group(conn: AsyncConnection, group_id: UUID) -> TaskGroup | None:
    rows = await fetch(
        conn, "SELECT id, name, created_at FROM task_groups WHERE id = :id", id=group_id
    )
    return _group(rows[0]) if rows else None


async def create_group(
    conn: AsyncConnection, *, tenant_id: UUID, user_id: UUID, name: str
) -> TaskGroup:
    rows = await fetch(
        conn,
        """INSERT INTO task_groups (id, tenant_id, name, created_by)
           VALUES (:id, :tenant, :name, :user) RETURNING id, name, created_at""",
        id=uuid4(),
        tenant=tenant_id,
        name=name,
        user=user_id,
    )
    return _group(rows[0])


async def rename_group(conn: AsyncConnection, group_id: UUID, name: str) -> TaskGroup | None:
    rows = await fetch(
        conn,
        "UPDATE task_groups SET name = :name WHERE id = :id RETURNING id, name, created_at",
        id=group_id,
        name=name,
    )
    return _group(rows[0]) if rows else None


async def delete_group(conn: AsyncConnection, group_id: UUID) -> None:
    """Its tasks stay, ungrouped (the foreign key clears their group)."""
    await execute(conn, "DELETE FROM task_groups WHERE id = :id", id=group_id)


async def update(conn: AsyncConnection, task_id: UUID, **fields: Any) -> None:
    """Updates the given columns. ``result`` is stored as JSON."""
    allowed = {
        "goal",
        "constraints",
        "status",
        "revision",
        "control_revision",
        "result",
        "permission_mode",
        "model",
        "reasoning_effort",
        "device_ids",
        "goal_paused",
        "plan_mode",
    }
    assert set(fields) <= allowed, set(fields) - allowed
    sets = ", ".join(
        f"{k} = CAST(:{k} AS jsonb)" if k == "result" else f"{k} = :{k}" for k in fields
    )
    params = {k: jsonb(v) if k == "result" else v for k, v in fields.items()}
    await execute(
        conn, f"UPDATE tasks SET {sets}, updated_at = now() WHERE id = :id", id=task_id, **params
    )


async def add_input(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    task_id: UUID,
    kind: str,
    text: str,
    revision: int,
    created_by: UUID | None,
    run_id: UUID | None = None,
) -> int:
    """``run_id``: the one run it goes to; none: the conversation."""
    row = await fetch_one(
        conn,
        """INSERT INTO task_inputs (tenant_id, task_id, seq, kind, text, revision, created_by,
                                    run_id)
           SELECT :tenant_id, :task_id,
                  -- Above every number ever read: a withdrawn message's is not used again,
                  -- so no step's cursor skips the next one.
                  GREATEST(
                    (SELECT COALESCE(MAX(seq), 0) FROM task_inputs WHERE task_id = :task_id),
                    (SELECT COALESCE(MAX(s.input_seq), 0) FROM steps s
                       JOIN runs r ON r.id = s.run_id WHERE r.task_id = :task_id),
                    (SELECT COALESCE(MAX(input_seq), 0) FROM side_turns
                      WHERE task_id = :task_id)
                  ) + 1,
                  :kind, :text, :revision, :created_by, :run_id
           RETURNING seq""",
        tenant_id=tenant_id,
        task_id=task_id,
        kind=kind,
        text=text,
        revision=revision,
        created_by=created_by,
        run_id=run_id,
    )
    assert row is not None
    return int(row["seq"])


async def inputs(
    conn: AsyncConnection, task_id: UUID, *, share: bool = False
) -> Sequence[RowMapping]:
    """``share``: held until the transaction ends, so a message cannot be withdrawn
    while a step that read it is being started."""
    return await fetch(
        conn,
        "SELECT seq, kind, text, revision, created_at, run_id, created_by FROM task_inputs"
        " WHERE task_id = :t ORDER BY seq" + (" FOR SHARE" if share else ""),
        t=task_id,
    )


async def delete_input(conn: AsyncConnection, task_id: UUID, seq: int) -> bool:
    count = await execute(
        conn, "DELETE FROM task_inputs WHERE task_id = :t AND seq = :s", t=task_id, s=seq
    )
    return count == 1


# ---------------------------------------------------------------- events


async def emit(conn: AsyncConnection, task: Task | UUID, type: str, **data: Any) -> int:
    """Appends an event in the caller's transaction. Sequence numbers have no gaps.

    The stream head lives on the task row, so events of one task are numbered in
    commit order and a reader resuming from a cursor never skips a late commit.
    """
    task_id = task.id if isinstance(task, Task) else task
    row = await fetch_one(
        conn,
        """WITH head AS (
             UPDATE tasks SET event_seq = event_seq + 1 WHERE id = :t
             RETURNING tenant_id, event_seq)
           INSERT INTO events (tenant_id, task_id, seq, type, data)
           SELECT tenant_id, :t, event_seq, :type, CAST(:data AS jsonb) FROM head
           RETURNING seq""",
        t=task_id,
        type=type,
        data=json.dumps(data, default=str, ensure_ascii=False),
    )
    assert row is not None
    return int(row["seq"])


async def events_after(
    conn: AsyncConnection, task_id: UUID, after: int, limit: int = 500
) -> Sequence[RowMapping]:
    return await fetch(
        conn,
        "SELECT seq, type, data, created_at FROM events"
        " WHERE task_id = :t AND seq > :after ORDER BY seq LIMIT :limit",
        t=task_id,
        after=after,
        limit=limit,
    )


async def questions(conn: AsyncConnection, task_id: UUID) -> Sequence[RowMapping]:
    """The agents' questions in the task, oldest first: ``run_id``, ``text``, ``to``."""
    return await fetch(
        conn,
        """SELECT seq, created_at, (data->>'run_id')::uuid AS run_id, data->>'text' AS text,
                  COALESCE(data->>'to', 'user') AS "to"
             FROM events WHERE task_id = :t AND type = 'agent.question' ORDER BY seq""",
        t=task_id,
    )


async def work_spans(
    conn: AsyncConnection, task_id: UUID, now: datetime
) -> tuple[list[tuple[datetime, datetime]], bool]:
    """When the task's agent was working: its model calls, its inputs on the phones and
    its screenshots, each from start to end (to now if still under way). Also whether
    something is under way right now."""
    rows = await fetch(
        conn,
        """SELECT s.started_at AS a, s.finished_at AS b, s.status = 'sent' AS open
             FROM steps s JOIN runs r ON r.id = s.run_id WHERE r.task_id = :t
           UNION ALL
           SELECT sent_at, finished_at, status = 'sent' FROM actions
            WHERE task_id = :t AND sent_at IS NOT NULL
           UNION ALL
           SELECT requested_at, captured_at, status IN ('requested', 'sent') FROM observations
            WHERE task_id = :t AND status <> 'failed'""",
        t=task_id,
    )
    spans = [(r["a"], r["b"] or now) for r in rows if r["b"] is not None or r["open"]]
    return spans, any(r["open"] and r["b"] is None for r in rows)


async def goal_changes(conn: AsyncConnection, task_id: UUID) -> list[dict[str, Any]]:
    """The goal's changes in order: set (with the goal before), clear, pause, resume."""
    rows = await fetch(
        conn,
        """SELECT type, data, created_at FROM events WHERE task_id = :t
              AND type IN ('task.goal', 'goal.paused', 'goal.resumed') ORDER BY seq""",
        t=task_id,
    )
    changes = []
    for r in rows:
        data = r["data"] if isinstance(r["data"], dict) else {}
        kind = (
            "pause"
            if r["type"] == "goal.paused"
            else "resume"
            if r["type"] == "goal.resumed"
            else "set"
            if data.get("goal")
            else "clear"
        )
        changes.append(
            {"at": r["created_at"], "kind": kind, "previous": data.get("previous") or ""}
        )
    return changes


async def goal_progress(conn: AsyncConnection, task_id: UUID) -> tuple[int, datetime]:
    """How far the current goal is: the times it was evaluated since it was set, and when
    it was set (the task's start, if the first message set it)."""
    row = await fetch_one(
        conn,
        """WITH last AS (SELECT seq, created_at FROM events
                             WHERE task_id = :t AND type = 'task.goal' ORDER BY seq DESC LIMIT 1)
           SELECT COALESCE((SELECT created_at FROM last),
                           (SELECT created_at FROM tasks WHERE id = :t)) AS at,
                  (SELECT count(*) FROM events WHERE task_id = :t AND type = 'goal.evaluated'
                      AND seq > COALESCE((SELECT seq FROM last), 0)) AS evaluations""",
        t=task_id,
    )
    assert row is not None
    return int(row["evaluations"]), row["at"]


async def plan_proposed(conn: AsyncConnection, task_id: UUID) -> bool:
    """Whether the agent has presented a plan since plan mode was last turned on."""
    row = await fetch_one(
        conn,
        """SELECT EXISTS (
             SELECT 1 FROM events WHERE task_id = :t AND type = 'plan.proposed'
                AND seq > COALESCE((SELECT max(seq) FROM events WHERE task_id = :t
                                       AND type = 'task.plan_mode'
                                       AND (data->>'on')::boolean), 0)) AS yes""",
        t=task_id,
    )
    return bool(row and row["yes"])


async def goal_ever_set(conn: AsyncConnection, task_id: UUID) -> bool:
    """Whether the task's goal was ever set or cleared after it started."""
    row = await fetch_one(
        conn,
        "SELECT EXISTS (SELECT 1 FROM events WHERE task_id = :t AND type = 'task.goal') AS yes",
        t=task_id,
    )
    return bool(row and row["yes"])


async def last_event_seq(conn: AsyncConnection, task_id: UUID) -> int:
    row = await fetch_one(conn, "SELECT event_seq FROM tasks WHERE id = :t", t=task_id)
    return int(row["event_seq"]) if row else 0


# A task whose title could not be made after this many tries keeps showing its goal.
TITLE_TRIES = 3


async def untitled(conn: AsyncConnection, limit: int = 10) -> list[Task]:
    """Tasks still without a title, newest first (those are the ones people see)."""
    rows = await fetch(
        conn,
        f"""SELECT {_TASK} FROM tasks
             WHERE title IS NULL AND deleted_at IS NULL AND title_tries < :tries
             ORDER BY created_at DESC LIMIT :limit""",
        tries=TITLE_TRIES,
        limit=limit,
    )
    return [_task(r) for r in rows]


async def set_title(conn: AsyncConnection, task_id: UUID, title: str | None) -> None:
    """Stores the title, or counts a failed try (title None)."""
    if title is None:
        await execute(
            conn, "UPDATE tasks SET title_tries = title_tries + 1 WHERE id = :id", id=task_id
        )
    else:
        await execute(
            conn,
            # Titled outside any transaction: the task may have been deleted meanwhile.
            "UPDATE tasks SET title = :t WHERE id = :id AND title IS NULL AND deleted_at IS NULL",
            t=title,
            id=task_id,
        )


async def creator(conn: AsyncConnection, task_id: UUID) -> UUID:
    """Who started the task."""
    row = await fetch_one(conn, "SELECT created_by FROM tasks WHERE id = :id", id=task_id)
    assert row is not None
    created_by: UUID = row["created_by"]
    return created_by
