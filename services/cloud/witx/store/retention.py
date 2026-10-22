"""Deletion and retention: tombstones, the purge of a deleted task's content,
and the expiry of screenshots, tombstones and idempotency entries. Everything here runs
under one tenant's row-level security."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.store import notices, runs
from witx.store.db import execute, fetch, fetch_one, jsonb


async def deletion_facts(conn: AsyncConnection, task_id: UUID) -> RowMapping | None:
    """What decides whether a task may be deleted; locks the task row."""
    return await fetch_one(
        conn,
        """SELECT created_by, status, deleted_at,
                  EXISTS (SELECT 1 FROM actions a
                           WHERE a.task_id = t.id AND a.effect = 'unknown') AS unknown_pending
             FROM tasks t WHERE id = :t FOR UPDATE""",
        t=task_id,
    )


async def audit_summary(conn: AsyncConnection, task_id: UUID) -> dict[str, Any]:
    """The minimal record kept after deletion: counts and usage, no text or images."""
    actions = await fetch(
        conn,
        """SELECT kind, effect, count(*) AS n FROM actions WHERE task_id = :t
            GROUP BY kind, effect ORDER BY kind, effect""",
        t=task_id,
    )
    approvals = await fetch(
        conn,
        """SELECT coalesce(ap.decision, 'undecided') AS decision, count(*) AS n
             FROM approvals ap JOIN actions a ON a.id = ap.action_id
            WHERE a.task_id = :t GROUP BY 1 ORDER BY 1""",
        t=task_id,
    )
    return {
        "actions": [{"kind": r["kind"], "effect": r["effect"], "count": r["n"]} for r in actions],
        "approvals": {r["decision"]: r["n"] for r in approvals},
        "usage": {k: int(v) for k, v in (await runs.task_usage(conn, task_id)).items()},
    }


async def mark_deleted(
    conn: AsyncConnection,
    task_id: UUID,
    *,
    user_id: UUID | None,
    audit: dict[str, Any],
    now: datetime,
) -> None:
    row = await fetch_one(
        conn,
        """UPDATE tasks SET deleted_at = :now, deleted_by = :u, audit = CAST(:audit AS jsonb)
            WHERE id = :t AND deleted_at IS NULL RETURNING tenant_id""",
        t=task_id,
        u=user_id,
        audit=jsonb(audit),
        now=now,
    )
    if row is not None:
        await notices.notify(conn, "task", task_id=task_id, tenant_id=row["tenant_id"])


async def tasks_to_purge(conn: AsyncConnection, limit: int) -> list[UUID]:
    rows = await fetch(
        conn,
        """SELECT id FROM tasks WHERE deleted_at IS NOT NULL AND purged_at IS NULL
            ORDER BY deleted_at LIMIT :limit""",
        limit=limit,
    )
    return [r["id"] for r in rows]


async def lock_for_purge(conn: AsyncConnection, task_id: UUID) -> list[UUID] | None:
    """Locks a deleted, unpurged task and its observations (so nothing completes them
    meanwhile). Returns their ids, or None if another worker has it. All of them, not
    only those with a screenshot on record: one whose upload failed its check still has
    the uploaded object."""
    task = await fetch_one(
        conn,
        """SELECT 1 FROM tasks WHERE id = :t AND deleted_at IS NOT NULL AND purged_at IS NULL
           FOR UPDATE SKIP LOCKED""",
        t=task_id,
    )
    if task is None:
        return None
    rows = await fetch(
        conn,
        "SELECT id FROM observations WHERE task_id = :t FOR UPDATE",
        t=task_id,
    )
    return [r["id"] for r in rows]


async def purge_task_rows(conn: AsyncConnection, task_id: UUID, now: datetime) -> None:
    """Deletes everything the task produced; the tombstone keeps only ids, times, status,
    model and the audit summary."""
    task_runs = "(SELECT id FROM runs WHERE task_id = :t)"
    for statement in (
        # A stored response repeats the task's text. A retry of that request is told the
        # task is gone (rather than the key being dropped, which would let a late retry
        # create the task again).
        """UPDATE idempotency_keys SET response = 'null'::jsonb
            WHERE response->>'id' = CAST(:t AS text)
               OR split_part(request, '/', 5) IN (
                    SELECT CAST(:t AS text)
                    UNION ALL SELECT CAST(id AS text) FROM actions WHERE task_id = :t
                    UNION ALL SELECT CAST(id AS text) FROM purchase_intents WHERE task_id = :t
                    UNION ALL SELECT CAST(id AS text) FROM grants WHERE task_id = :t)""",
        "DELETE FROM approvals WHERE action_id IN (SELECT id FROM actions WHERE task_id = :t)",
        "DELETE FROM grants WHERE task_id = :t",
        # Kept memories stay the person's, also what an agent kept on its own;
        # proposals from before D59, never kept, go with the task.
        "DELETE FROM user_memories WHERE source_task_id = :t AND status <> 'kept'",
        "UPDATE user_memories SET source_task_id = NULL WHERE source_task_id = :t",
        "DELETE FROM actions WHERE task_id = :t",
        "DELETE FROM purchase_intents WHERE task_id = :t",
        f"DELETE FROM tool_calls WHERE run_id IN {task_runs}",
        f"DELETE FROM steps WHERE run_id IN {task_runs}",
        f"DELETE FROM run_summaries WHERE run_id IN {task_runs}",
        "DELETE FROM task_inputs WHERE task_id = :t",  # before the runs they may name
        "DELETE FROM side_turns WHERE task_id = :t",
        "DELETE FROM device_leases WHERE task_id = :t",
        "DELETE FROM runs WHERE task_id = :t",
        "DELETE FROM observations WHERE task_id = :t",
        "DELETE FROM events WHERE task_id = :t",
        "DELETE FROM side_messages WHERE task_id = :t",
        "DELETE FROM task_pins WHERE task_id = :t",
    ):
        await execute(conn, statement, t=task_id)
    await execute(
        conn,
        """UPDATE tasks SET goal = '', request = '', title = NULL, constraints = '{}',
                  result = NULL, purged_at = :now
            WHERE id = :t""",
        t=task_id,
        now=now,
    )


async def expired_screenshots(
    conn: AsyncConnection, *, now: datetime, days: int, limit: int
) -> list[tuple[UUID, str]]:
    """Screenshots past retention, locked: ``days``, the deployment's, or fewer where
    the person who started the task chose fewer. Kept: those of unfinished tasks
    (the model may still look at them) and the evidence of an action whose effect is
    unknown."""
    rows = await fetch(
        conn,
        """SELECT o.id, o.object_key FROM observations o
             LEFT JOIN tasks t ON t.id = o.task_id
             LEFT JOIN memberships m ON m.tenant_id = t.tenant_id AND m.user_id = t.created_by
            WHERE o.object_key IS NOT NULL
              AND o.captured_at < :now - make_interval(
                    days => least(coalesce(m.screenshot_retention_days, :days), :days))
              -- No task (a phone taken over from the pool): the deployment's time.
              AND (t.id IS NULL OR t.status IN ('stopped', 'completed', 'failed'))
              AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.effect = 'unknown'
                               AND o.id IN (a.observation_id, a.post_observation_id))
            ORDER BY o.captured_at LIMIT :limit
              FOR UPDATE OF o SKIP LOCKED""",
        now=now,
        days=days,
        limit=limit,
    )
    return [(r["id"], r["object_key"]) for r in rows]


async def screenshots_held(conn: AsyncConnection, ids: list[UUID]) -> set[UUID]:
    """Of these observations, those whose screenshot object is still theirs: on record,
    or not settled yet (its upload may be under way)."""
    rows = await fetch(
        conn,
        """SELECT id FROM observations WHERE id = ANY(:ids)
              AND (object_key IS NOT NULL OR status IN ('requested', 'sent'))""",
        ids=ids,
    )
    return {r["id"] for r in rows}


async def record_sweep(
    conn: AsyncConnection, tenant_id: UUID, *, finished_at: datetime, counts: dict[str, Any]
) -> None:
    """Keeps the tenant's last sweep, for /metrics."""
    await execute(
        conn,
        """INSERT INTO screenshot_sweeps (tenant_id, finished_at, duration_ms, scanned, deleted,
                  skipped_held, skipped_unrecognized, failed, error)
           VALUES (:t, :at, :duration_ms, :scanned, :deleted, :skipped_held,
                   :skipped_unrecognized, :failed, :error)
           ON CONFLICT (tenant_id) DO UPDATE SET
                  finished_at = EXCLUDED.finished_at, duration_ms = EXCLUDED.duration_ms,
                  scanned = EXCLUDED.scanned, deleted = EXCLUDED.deleted,
                  skipped_held = EXCLUDED.skipped_held,
                  skipped_unrecognized = EXCLUDED.skipped_unrecognized,
                  failed = EXCLUDED.failed, error = EXCLUDED.error""",
        t=tenant_id,
        at=finished_at,
        **counts,
    )


async def mark_screenshots_purged(conn: AsyncConnection, ids: list[UUID]) -> None:
    await execute(conn, "UPDATE observations SET object_key = NULL WHERE id = ANY(:ids)", ids=ids)


async def drop_tombstones(conn: AsyncConnection, *, before: datetime) -> int:
    return await execute(
        conn,
        "DELETE FROM tasks WHERE purged_at IS NOT NULL AND deleted_at < :before",
        before=before,
    )


async def drop_idempotency_keys(conn: AsyncConnection, *, before: datetime) -> int:
    return await execute(
        conn, "DELETE FROM idempotency_keys WHERE created_at < :before", before=before
    )
