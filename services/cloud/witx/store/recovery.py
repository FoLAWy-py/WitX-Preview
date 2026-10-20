"""The changes `witx admin recover` makes to a database restored from a backup.

All of it runs on the owner's connection, across tenants, before the API and the workers
start on the restored database.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.store.db import execute, fetch, fetch_one

RESTORED = "restored_from_backup"
# Far above any epoch a device may have reached after the backup was taken, so the
# Gateway (whose journal was not restored) accepts the leases issued from now on.
EPOCH_JUMP = 1_000_000


async def user_exists(conn: AsyncConnection, user_id: Any) -> bool:
    if user_id is None:
        return False
    return await fetch_one(conn, "SELECT 1 FROM users WHERE id = :u", u=user_id) is not None


async def task_to_delete(conn: AsyncConnection, task_id: UUID) -> bool:
    row = await fetch_one(
        conn, "SELECT 1 FROM tasks WHERE id = :t AND deleted_at IS NULL", t=task_id
    )
    return row is not None


async def device_state(conn: AsyncConnection, device_id: UUID) -> dict[str, Any] | None:
    row = await fetch_one(
        conn,
        "SELECT name, enabled_at IS NOT NULL AS enabled FROM devices WHERE id = :d",
        d=device_id,
    )
    return None if row is None else dict(row)


async def gateway_state(conn: AsyncConnection, gateway_id: UUID) -> str | None:
    row = await fetch_one(
        conn,
        """SELECT CASE WHEN revoked_at IS NOT NULL THEN 'revoked'
                       WHEN disabled_at IS NOT NULL THEN 'disabled' ELSE 'enabled' END AS state
             FROM gateways WHERE id = :g""",
        g=gateway_id,
    )
    return None if row is None else str(row["state"])


async def stop_unfinished(conn: AsyncConnection, now: datetime) -> dict[str, int]:
    """Nothing restored may run on: unfinished tasks stop, unsent input is cancelled,
    input sent without a result becomes unknown (a person settles it from a screenshot),
    leases go, device epochs jump, and every sign-in session ends."""
    stopped = await fetch(
        conn,
        """UPDATE tasks SET status = 'stopped', updated_at = :now,
                  result = COALESCE(result, '{}'::jsonb)
                           || jsonb_build_object('summary', 'Stopped: restored from a backup.')
            WHERE status NOT IN ('stopped', 'completed', 'failed') AND deleted_at IS NULL
        RETURNING id""",
        now=now,
    )
    await execute(
        conn,
        """UPDATE runs SET status = 'cancelled', updated_at = :now
            WHERE status NOT IN ('completed', 'failed', 'cancelled')""",
        now=now,
    )
    cancelled = await fetch(
        conn,
        """UPDATE actions SET status = 'cancelled', reason = :r
            WHERE status IN ('queued', 'awaiting_approval') RETURNING id""",
        r=RESTORED,
    )
    unknown = await fetch(
        conn,
        """UPDATE actions SET status = 'done', effect = 'unknown', reason = :r, finished_at = :now
            WHERE status = 'sent' RETURNING id""",
        r=RESTORED,
        now=now,
    )
    await execute(
        conn,
        """UPDATE observations SET status = 'failed', error = :r
            WHERE status IN ('requested', 'sent')""",
        r=RESTORED,
    )
    await execute(conn, "DELETE FROM device_leases")
    devices = await fetch(
        conn,
        "UPDATE devices SET epoch = epoch + :jump, online = false RETURNING id",
        jump=EPOCH_JUMP,
    )
    sessions = await fetch(conn, "DELETE FROM sessions RETURNING 1 AS gone")
    return {
        "tasks_stopped": len(stopped),
        "inputs_cancelled": len(cancelled),
        "inputs_unknown": len(unknown),
        "device_epochs_raised": len(devices),
        "sessions_ended": len(sessions),
    }
