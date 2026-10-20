"""Runs, model steps and tool calls."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import Run, RunStatus, RunSummary, Saying, Step, ToolCall, WaitReason
from witx.store.db import execute, fetch, fetch_one, jsonb

_RUN = """id, tenant_id, task_id, parent_run_id, goal, status, wait_reason, wait_detail,
          epoch, depth, device_ids, result, created_at, spawn_key, name, saying, phase"""


def _run(row: RowMapping) -> Run:
    return Run(
        id=row["id"],
        tenant_id=row["tenant_id"],
        task_id=row["task_id"],
        parent_run_id=row["parent_run_id"],
        goal=row["goal"],
        status=RunStatus(row["status"]),
        wait_reason=WaitReason(row["wait_reason"]) if row["wait_reason"] else None,
        wait_detail=row["wait_detail"],
        epoch=row["epoch"],
        depth=row["depth"],
        device_ids=list(row["device_ids"]),
        result=row["result"],
        created_at=row["created_at"],
        spawn_key=row["spawn_key"],
        name=row["name"],
        saying=row["saying"],
        phase=row["phase"],
    )


async def set_saying(
    conn: AsyncConnection, run_id: UUID, epoch: int, text: str | None, phase: str | None
) -> bool:
    """What the run is saying right now and what its step is at, or None and None
    once the step is over. Only the worker that holds the claim (``epoch``) may write
    it."""
    count = await execute(
        conn,
        "UPDATE runs SET saying = :text, phase = :phase WHERE id = :id AND epoch = :epoch",
        id=run_id,
        epoch=epoch,
        text=text,
        phase=phase,
    )
    return count == 1


async def saying_of_task(conn: AsyncConnection, task_id: UUID) -> dict[UUID, Saying]:
    """What each run of the task is saying right now and what it is at, by run ID."""
    rows = await fetch(
        conn,
        "SELECT id, saying, phase FROM runs"
        " WHERE task_id = :t AND (saying IS NOT NULL OR phase IS NOT NULL)",
        t=task_id,
    )
    return {row["id"]: Saying(row["saying"] or "", row["phase"]) for row in rows}


async def insert(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    task_id: UUID,
    goal: str,
    device_ids: list[UUID],
    parent_run_id: UUID | None = None,
    spawn_key: str | None = None,
    depth: int = 0,
    name: str | None = None,
) -> Run | None:
    """Creates a runnable run. Returns None if (parent, spawn_key) already exists."""
    row = await fetch_one(
        conn,
        f"""INSERT INTO runs (id, tenant_id, task_id, parent_run_id, spawn_key, goal, depth,
                              device_ids, status, name)
            VALUES (:id, :tenant_id, :task_id, :parent, :spawn_key, :goal, :depth, :devices,
                    'runnable', :name)
            ON CONFLICT (parent_run_id, spawn_key) DO NOTHING
            RETURNING {_RUN}""",
        id=id,
        tenant_id=tenant_id,
        task_id=task_id,
        parent=parent_run_id,
        spawn_key=spawn_key,
        goal=goal,
        depth=depth,
        devices=device_ids,
        name=name,
    )
    return _run(row) if row else None


async def get(conn: AsyncConnection, run_id: UUID, *, lock: bool = False) -> Run | None:
    row = await fetch_one(
        conn, f"SELECT {_RUN} FROM runs WHERE id = :id" + (" FOR UPDATE" if lock else ""), id=run_id
    )
    return _run(row) if row else None


async def for_task(conn: AsyncConnection, task_id: UUID) -> list[Run]:
    rows = await fetch(
        conn, f"SELECT {_RUN} FROM runs WHERE task_id = :t ORDER BY created_at", t=task_id
    )
    return [_run(r) for r in rows]


async def children(conn: AsyncConnection, run_id: UUID) -> list[Run]:
    rows = await fetch(
        conn, f"SELECT {_RUN} FROM runs WHERE parent_run_id = :r ORDER BY created_at", r=run_id
    )
    return [_run(r) for r in rows]


async def read_through(conn: AsyncConnection, task_id: UUID) -> int:
    """The newest input a step of the main agent has read (or is reading now)."""
    row = await fetch_one(
        conn,
        """SELECT COALESCE(MAX(s.input_seq), 0) AS seq FROM steps s JOIN runs r ON r.id = s.run_id
            WHERE r.task_id = :t AND r.parent_run_id IS NULL AND s.status IN ('sent', 'done')""",
        t=task_id,
    )
    return int(row["seq"]) if row else 0


async def delegating(conn: AsyncConnection, run_id: UUID) -> bool:
    """Whether the run waits in a delegate call: one of its calls has sub-agents and no
    result yet (whatever the run's own state while a worker rechecks it)."""
    row = await fetch_one(
        conn,
        """SELECT EXISTS (SELECT 1 FROM tool_calls
                           WHERE run_id = :r AND child_run_id IS NOT NULL AND output IS NULL)
                  AS waiting""",
        r=run_id,
    )
    return bool(row and row["waiting"])


async def claim(
    conn: AsyncConnection, owner: str, lease_seconds: int, limit: int
) -> list[tuple[UUID, UUID, int]]:
    rows = await fetch(
        conn, "SELECT * FROM witx_claim_runs(:o, :s, :n)", o=owner, s=lease_seconds, n=limit
    )
    return [(r["run_id"], r["tenant_id"], r["epoch"]) for r in rows]


async def set_state(
    conn: AsyncConnection,
    run_id: UUID,
    status: RunStatus,
    *,
    epoch: int | None = None,
    wait_reason: WaitReason | None = None,
    wait_detail: str | None = None,
    next_wake_at: datetime | None = None,
    result: dict[str, Any] | None = None,
) -> bool:
    """Moves a run to a new state.

    With ``epoch`` the update only applies if the caller still holds the claim, so a
    worker whose lease was taken over cannot overwrite newer state.
    """
    fence = " AND epoch = :epoch AND status = 'running'" if epoch is not None else ""
    count = await execute(
        conn,
        f"""UPDATE runs SET status = :status, wait_reason = :reason, wait_detail = :detail,
                   next_wake_at = :wake, result = COALESCE(CAST(:result AS jsonb), result),
                   owner = NULL, lease_until = NULL, updated_at = now()
             WHERE id = :id{fence}""",
        id=run_id,
        status=status.value,
        reason=wait_reason.value if wait_reason else None,
        detail=wait_detail,
        wake=next_wake_at,
        result=jsonb(result),
        epoch=epoch,
    )
    return count == 1


async def wake_task(conn: AsyncConnection, task_id: UUID, reasons: Iterable[WaitReason]) -> None:
    """Makes the task's runs that wait for one of ``reasons`` runnable."""
    await execute(
        conn,
        """UPDATE runs SET status = 'runnable', wait_reason = NULL, wait_detail = NULL,
                  next_wake_at = NULL, updated_at = now()
            WHERE task_id = :t AND status = 'waiting' AND wait_reason = ANY(:reasons)""",
        t=task_id,
        reasons=[r.value for r in reasons],
    )


async def wake(conn: AsyncConnection, run_id: UUID, reasons: Iterable[WaitReason]) -> bool:
    """Makes a waiting run runnable if it waits for one of ``reasons``."""
    count = await execute(
        conn,
        """UPDATE runs SET status = 'runnable', wait_reason = NULL, wait_detail = NULL,
                  next_wake_at = NULL, updated_at = now()
            WHERE id = :id AND status = 'waiting' AND wait_reason = ANY(:reasons)""",
        id=run_id,
        reasons=[r.value for r in reasons],
    )
    return count == 1


async def wake_for_devices(conn: AsyncConnection, device_ids: list[UUID]) -> list[Run]:
    """Makes the runs that wait for a device (``WaitReason.DEVICE``) runnable when they
    use one of these, so they look again whether they can go on. Returns them."""
    if not device_ids:
        return []
    rows = await fetch(
        conn,
        f"""UPDATE runs SET status = 'runnable', wait_reason = NULL, wait_detail = NULL,
                   next_wake_at = NULL, updated_at = now()
             WHERE status = 'waiting' AND wait_reason = 'device'
               AND device_ids && CAST(:ids AS uuid[])
            RETURNING {_RUN}""",
        ids=device_ids,
    )
    return [_run(r) for r in rows]


async def wake_after_model_error(conn: AsyncConnection, task_id: UUID) -> int:
    """Makes runs that wait because of the model (a retry, or a failure handed to the
    user) runnable again."""
    return await execute(
        conn,
        """UPDATE runs SET status = 'runnable', wait_reason = NULL, wait_detail = NULL,
                  next_wake_at = NULL, updated_at = now()
            WHERE task_id = :t AND status = 'waiting'
              AND (wait_reason = 'model_retry'
                   OR (wait_reason = 'user_input' AND wait_detail LIKE 'model error:%'))""",
        t=task_id,
    )


async def heartbeat(conn: AsyncConnection, run_id: UUID, epoch: int, lease_seconds: int) -> bool:
    count = await execute(
        conn,
        """UPDATE runs SET lease_until = now() + make_interval(secs => :s)
            WHERE id = :id AND epoch = :epoch AND status = 'running'""",
        id=run_id,
        epoch=epoch,
        s=lease_seconds,
    )
    return count == 1


# ---------------------------------------------------------------- steps


def _step(row: RowMapping) -> Step:
    return Step(
        id=row["id"],
        run_id=row["run_id"],
        seq=row["seq"],
        status=row["status"],
        input_seq=row["input_seq"],
        input_items=list(row["input_items"]),
        output_items=list(row["output_items"] or []),
        goal_check=row.get("goal_check"),
    )


async def start_step(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    run_id: UUID,
    model: str,
    input_items: list[dict[str, Any]],
    input_seq: int,
    request: dict[str, Any],
) -> Step:
    """Records that a model request is about to be sent. Earlier unfinished steps of the
    run are marked abandoned: their requests are never sent again."""
    await execute(
        conn,
        "UPDATE steps SET status = 'abandoned', finished_at = now()"
        " WHERE run_id = :r AND status = 'sent'",
        r=run_id,
    )
    row = await fetch_one(
        conn,
        """INSERT INTO steps (id, tenant_id, run_id, seq, status, model, input_seq, input_items,
                              request)
           SELECT :id, :tenant_id, :run_id, COALESCE(MAX(seq), 0) + 1, 'sent', :model,
                  :input_seq, CAST(:items AS jsonb), CAST(:request AS jsonb)
             FROM steps WHERE run_id = :run_id
           RETURNING id, run_id, seq, status, input_seq, input_items, output_items""",
        id=id,
        tenant_id=tenant_id,
        run_id=run_id,
        model=model,
        input_seq=input_seq,
        items=jsonb(input_items),
        request=jsonb(request),
    )
    assert row is not None
    return _step(row)


async def finish_step(
    conn: AsyncConnection,
    step_id: UUID,
    *,
    status: str,
    response: dict[str, Any] | None = None,
    output_items: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    risk_call: dict[str, Any] | None = None,
    verify_call: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await execute(
        conn,
        """UPDATE steps SET status = :status, response = CAST(:response AS jsonb),
                  output_items = CAST(:items AS jsonb), usage = CAST(:usage AS jsonb),
                  risk_call = CAST(:risk_call AS jsonb),
                  verify_call = CAST(:verify_call AS jsonb), error = :error,
                  finished_at = now()
            WHERE id = :id""",
        id=step_id,
        status=status,
        response=jsonb(response),
        items=jsonb(output_items),
        usage=jsonb(usage),
        risk_call=jsonb(risk_call),
        verify_call=jsonb(verify_call),
        error=error,
    )


async def completed_steps(conn: AsyncConnection, run_id: UUID) -> list[Step]:
    """Steps whose response was accepted, in order: the run's conversation history."""
    rows = await fetch(
        conn,
        """SELECT id, run_id, seq, status, input_seq, input_items, output_items,
                  verify_call->'goal' AS goal_check FROM steps
            WHERE run_id = :r AND status = 'done' ORDER BY seq""",
        r=run_id,
    )
    return [_step(r) for r in rows]


async def task_usage(conn: AsyncConnection, task_id: UUID) -> dict[str, int]:
    """Provider-reported token usage summed over every model call of a task, the
    independent risk and completion checks, the context summaries and the main agent's
    side turns included."""
    row = await fetch_one(
        conn,
        """SELECT count(*) + count(s.risk_call) + count(s.verify_call) AS model_calls,
                  COALESCE(SUM((s.usage->>'input_tokens')::bigint), 0)
                    + COALESCE(SUM((s.risk_call->'usage'->>'input_tokens')::bigint), 0)
                    + COALESCE(SUM((s.verify_call->'usage'->>'input_tokens')::bigint), 0)
                    AS input_tokens,
                  COALESCE(SUM((s.usage->>'output_tokens')::bigint), 0)
                    + COALESCE(SUM((s.risk_call->'usage'->>'output_tokens')::bigint), 0)
                    + COALESCE(SUM((s.verify_call->'usage'->>'output_tokens')::bigint), 0)
                    AS output_tokens
             FROM steps s JOIN runs r ON r.id = s.run_id
            WHERE r.task_id = :t AND s.status IN ('done', 'abandoned')""",
        t=task_id,
    )
    assert row is not None
    summaries = await fetch_one(
        conn,
        """SELECT count(m.call) AS model_calls,
                  COALESCE(SUM((m.call->'usage'->>'input_tokens')::bigint), 0) AS input_tokens,
                  COALESCE(SUM((m.call->'usage'->>'output_tokens')::bigint), 0) AS output_tokens
             FROM run_summaries m JOIN runs r ON r.id = m.run_id WHERE r.task_id = :t""",
        t=task_id,
    )
    assert summaries is not None
    side = await fetch_one(
        conn,
        """SELECT count(t.call->'usage') AS model_calls,
                  COALESCE(SUM((t.call->'usage'->>'input_tokens')::bigint), 0) AS input_tokens,
                  COALESCE(SUM((t.call->'usage'->>'output_tokens')::bigint), 0) AS output_tokens
             FROM side_turns t WHERE t.task_id = :t""",
        t=task_id,
    )
    assert side is not None
    return {
        k: int(row[k]) + int(summaries[k]) + int(side[k])
        for k in ("model_calls", "input_tokens", "output_tokens")
    }


async def failed_models_since_success(conn: AsyncConnection, run_id: UUID) -> list[str]:
    """The model of each step that failed since the run's last successful one, in order."""
    rows = await fetch(
        conn,
        """SELECT model FROM steps
            WHERE run_id = :r AND status = 'error'
              AND seq > COALESCE((SELECT MAX(seq) FROM steps WHERE run_id = :r
                                  AND status = 'done'), 0)
            ORDER BY seq""",
        r=run_id,
    )
    return [r["model"] for r in rows]


# ---------------------------------------------------------------- tool calls


def _call(row: RowMapping) -> ToolCall:
    return ToolCall(
        id=row["id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        call_id=row["call_id"],
        name=row["name"],
        arguments=dict(row["arguments"]),
        output=row["output"],
        observation_id=row["observation_id"],
        action_id=row["action_id"],
        child_run_id=row["child_run_id"],
    )


_CALL = (
    "id, run_id, step_id, call_id, name, arguments, output, observation_id, action_id, child_run_id"
)


async def insert_call(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    run_id: UUID,
    step_id: UUID,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
) -> ToolCall:
    row = await fetch_one(
        conn,
        f"""INSERT INTO tool_calls (id, tenant_id, run_id, step_id, call_id, name, arguments)
            VALUES (:id, :tenant_id, :run_id, :step_id, :call_id, :name, CAST(:args AS jsonb))
            RETURNING {_CALL}""",
        id=id,
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=step_id,
        call_id=call_id,
        name=name,
        args=jsonb(arguments),
    )
    assert row is not None
    return _call(row)


async def link_call(conn: AsyncConnection, call_id: UUID, **refs: UUID) -> None:
    assert set(refs) <= {"observation_id", "action_id", "child_run_id"}
    sets = ", ".join(f"{k} = :{k}" for k in refs)
    await execute(conn, f"UPDATE tool_calls SET {sets} WHERE id = :id", id=call_id, **refs)


async def resolve_call(conn: AsyncConnection, call_id: UUID, output: dict[str, Any]) -> bool:
    """Sets the result of a tool call once. Returns False if it was already resolved."""
    count = await execute(
        conn,
        """UPDATE tool_calls SET output = CAST(:o AS jsonb), resolved_at = now()
            WHERE id = :id AND output IS NULL""",
        id=call_id,
        o=jsonb(output),
    )
    return count == 1


async def calls_for_run(conn: AsyncConnection, run_id: UUID) -> list[ToolCall]:
    rows = await fetch(
        conn,
        f"SELECT {_CALL} FROM tool_calls WHERE run_id = :r ORDER BY created_at, call_id",
        r=run_id,
    )
    return [_call(r) for r in rows]


async def call_by_call_id(conn: AsyncConnection, run_id: UUID, call_id: str) -> ToolCall | None:
    row = await fetch_one(
        conn,
        f"SELECT {_CALL} FROM tool_calls WHERE run_id = :r AND call_id = :c",
        r=run_id,
        c=call_id,
    )
    return _call(row) if row else None


async def call_by_ref(conn: AsyncConnection, column: str, value: UUID) -> ToolCall | None:
    assert column in {"observation_id", "action_id", "child_run_id"}
    row = await fetch_one(conn, f"SELECT {_CALL} FROM tool_calls WHERE {column} = :v", v=value)
    return _call(row) if row else None


async def set_device_ids(conn: AsyncConnection, run_id: UUID, device_ids: list[UUID]) -> None:
    await execute(conn, "UPDATE runs SET device_ids = :d WHERE id = :id", d=device_ids, id=run_id)


# ---------------------------------------------------------------- summaries


async def latest_summaries(conn: AsyncConnection, run_id: UUID) -> list[RunSummary]:
    """The run's newest summary attempt, and its newest one that worked (one row if
    they are the same; none before the first attempt)."""
    rows = await fetch(
        conn,
        """(SELECT id, run_id, through_seq, summary, created_at FROM run_summaries
              WHERE run_id = :r ORDER BY created_at DESC LIMIT 1)
           UNION
           (SELECT id, run_id, through_seq, summary, created_at FROM run_summaries
              WHERE run_id = :r AND summary IS NOT NULL ORDER BY created_at DESC LIMIT 1)""",
        r=run_id,
    )
    return sorted((RunSummary(**dict(r)) for r in rows), key=lambda s: s.created_at)


async def add_summary(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    run_id: UUID,
    through_seq: int,
    summary: str | None,
    call: dict[str, Any] | None,
) -> RunSummary:
    row = await fetch_one(
        conn,
        """INSERT INTO run_summaries (id, tenant_id, run_id, through_seq, summary, call)
           VALUES (:id, :t, :r, :s, :summary, CAST(:call AS jsonb))
           RETURNING id, run_id, through_seq, summary, created_at""",
        id=uuid4(),
        t=tenant_id,
        r=run_id,
        s=through_seq,
        summary=summary,
        call=jsonb(call),
    )
    assert row is not None
    return RunSummary(**dict(row))


async def last_input_tokens(conn: AsyncConnection, run_id: UUID) -> int | None:
    """The input size of the run's newest completed step, as the provider counted it."""
    row = await fetch_one(
        conn,
        """SELECT (usage->>'input_tokens')::int AS n FROM steps
            WHERE run_id = :r AND status = 'done' ORDER BY seq DESC LIMIT 1""",
        r=run_id,
    )
    return int(row["n"]) if row and row["n"] is not None else None


# ---------------------------------------------------------------- plans and notes


async def latest_plan_call(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """The arguments of the run's newest accepted update_plan call."""
    row = await fetch_one(
        conn,
        """SELECT c.arguments FROM tool_calls c JOIN steps s ON s.id = c.step_id
            WHERE c.run_id = :r AND c.name = 'update_plan' AND c.output->>'status' = 'ok'
            ORDER BY s.seq DESC, c.call_id DESC LIMIT 1""",
        r=run_id,
    )
    return dict(row["arguments"]) if row else None


async def task_notes(conn: AsyncConnection, task_id: UUID) -> list[RowMapping]:
    """What the agent wrote down in ``notes`` with its inputs, oldest first, with the
    screenshot each describes and when that screenshot was taken."""
    return list(
        await fetch(
            conn,
            """SELECT c.arguments->>'notes' AS text, o.id AS observation_id, o.captured_at
                 FROM tool_calls c
                 JOIN runs r ON r.id = c.run_id
                 JOIN steps s ON s.id = c.step_id
                 LEFT JOIN observations o
                        ON o.id::text = c.arguments->>'observation_id' AND o.task_id = r.task_id
                WHERE r.task_id = :t AND COALESCE(c.arguments->>'notes', '') <> ''
                ORDER BY s.started_at, c.call_id""",
            t=task_id,
        )
    )
