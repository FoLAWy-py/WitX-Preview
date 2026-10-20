"""Deleting a task and enforcing retention .

Deleting writes a tombstone (the task disappears at once) with a minimal audit summary.
The purge, run by the worker, then removes the task's screenshots and rows; the
tombstone is its retry queue and the observation rows are its list of objects, so an
interrupted purge just runs again. Screenshots expire after the screenshot retention,
tombstones after the audit retention.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import logs

from witx.domain import rules
from witx.domain.model import TaskStatus
from witx.domain.rules import Rejected
from witx.objects import ObjectStore
from witx.services import ledger
from witx.services.identity import Actor
from witx.store import identity, retention
from witx.store.db import Database

log = logging.getLogger(__name__)


async def delete_task(
    conn: AsyncConnection, actor: Actor, task_id: UUID, now: datetime, objects: ObjectStore
) -> None:
    """Deleting twice is the same as deleting once. The deletion is also written to the
    restore ledger first, so restoring an older backup cannot bring the task back."""
    facts = await retention.deletion_facts(conn, task_id)
    if facts is None:
        raise Rejected("not_found", "task not found")
    if facts["deleted_at"] is not None:
        return
    rules.check_task_deletable(
        status=TaskStatus(facts["status"]),
        unknown_pending=facts["unknown_pending"],
        is_creator=facts["created_by"] == actor.user_id,
        is_owner=actor.role == "owner",
    )
    await ledger.record(objects, "task", task_id, {"deleted": True, "by": actor.user_id})
    audit = await retention.audit_summary(conn, task_id)
    await retention.mark_deleted(conn, task_id, user_id=actor.user_id, audit=audit, now=now)


@dataclass
class Purged:
    tasks: int = 0
    screenshots: int = 0
    tombstones: int = 0
    idempotency_keys: int = 0


async def purge(
    db: Database,
    objects: ObjectStore,
    *,
    now: datetime,
    screenshot_retention: timedelta,
    audit_retention: timedelta,
    batch: int = 100,
) -> Purged:
    """One pass over every tenant, each under its own row-level security."""
    done = Purged()
    async with db.system() as conn:
        tenants = [row["id"] for row in await identity.tenants(conn)]
    for tenant_id in tenants:
        with logs.log_context(tenant_id=tenant_id):
            await _purge_tenant(
                db,
                objects,
                tenant_id,
                done,
                now=now,
                batch=batch,
                screenshot_retention=screenshot_retention,
                audit_retention=audit_retention,
            )
    return done


async def _purge_tenant(
    db: Database,
    objects: ObjectStore,
    tenant_id: UUID,
    done: Purged,
    *,
    now: datetime,
    batch: int,
    screenshot_retention: timedelta,
    audit_retention: timedelta,
) -> None:
    async with db.tenant(tenant_id) as conn:
        deleted = await retention.tasks_to_purge(conn, batch)
    for task_id in deleted:
        async with db.tenant(tenant_id) as conn:
            keys = await retention.lock_for_purge(conn, task_id)
            if keys is None:
                continue
            # Objects first, rows last: if this stops halfway, the rows still list
            # what is left to delete, and deleting a missing object is fine.
            for key in keys:
                await objects.delete(key)
            await retention.purge_task_rows(conn, task_id, now)
        done.tasks += 1
    async with db.tenant(tenant_id) as conn:
        expired = await retention.expired_screenshots(
            conn, now=now, days=screenshot_retention.days, limit=batch
        )
        for _, key in expired:
            await objects.delete(key)
        await retention.mark_screenshots_purged(conn, [i for i, _ in expired])
        done.screenshots += len(expired)
        done.tombstones += await retention.drop_tombstones(conn, before=now - audit_retention)
        done.idempotency_keys += await retention.drop_idempotency_keys(
            conn, before=now - rules.IDEMPOTENCY_TTL
        )
