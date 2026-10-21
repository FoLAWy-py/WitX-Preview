"""Deleting a task and enforcing retention .

Deleting writes a tombstone (the task disappears at once) with a minimal audit summary.
The purge, run by the worker, then removes the task's screenshots and rows; the
tombstone is its retry queue and the observation rows are its list of objects, so an
interrupted purge just runs again. Screenshots expire after the screenshot retention,
tombstones after the audit retention. Once a day a sweep deletes screenshot objects no
observation holds.
"""

import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection
from witx_contracts import logs

from witx.domain import rules
from witx.domain.model import TaskStatus
from witx.domain.rules import Rejected
from witx.objects import ObjectStore, screenshot_key
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
            observations = await retention.lock_for_purge(conn, task_id)
            if observations is None:
                continue
            # Objects first, rows last: if this stops halfway, the rows still list
            # what is left to delete, and deleting a missing object is fine.
            for observation_id in observations:
                await objects.delete(screenshot_key(tenant_id, observation_id))
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


@dataclass
class Swept:
    """What a sweep found: every object listed is deleted, skipped or failed."""

    scanned: int = 0
    deleted: int = 0
    skipped_held: int = 0  # an observation holds it
    skipped_unrecognized: int = 0  # not named like a screenshot
    failed: int = 0  # could not be deleted; the next sweep tries again
    error: str | None = None  # why a tenant's pass stopped early

    def add(self, other: "Swept") -> None:
        for name in ("scanned", "deleted", "skipped_held", "skipped_unrecognized", "failed"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.error = self.error or other.error

    def counts(self) -> dict[str, int]:
        return {k: v for k, v in vars(self).items() if k != "error"}


async def sweep_screenshots(db: Database, objects: ObjectStore) -> Swept:
    """Deletes the screenshot objects no observation holds: an upload that failed its
    check or arrived after its observation failed, and objects whose rows are gone
    (tasks purged before every object was deleted by id, #305; a database restored from
    an older backup). It lists before it reads the rows, so an upload under way is found
    held, and it is safe while everything runs.

    Nothing stops it but the list of tenants: an object that cannot be deleted is
    counted and left for the next sweep, a tenant whose pass fails is recorded with its
    error and the next tenant is swept. Each tenant's pass is logged and kept for
    /metrics."""
    async with db.system() as conn:
        tenants = [row["id"] for row in await identity.tenants(conn)]
    total = Swept()
    for tenant_id in tenants:
        with logs.log_context(tenant_id=tenant_id):
            started = time.monotonic()
            swept = await _sweep_tenant(db, objects, tenant_id)
            duration_ms = round((time.monotonic() - started) * 1000)
            fields = swept.counts() | {"duration_ms": duration_ms}
            level = logging.WARNING if swept.failed or swept.error else logging.INFO
            log.log(level, "screenshot sweep", extra=fields | {"error_code": swept.error})
            try:
                async with db.tenant(tenant_id) as conn:
                    await retention.record_sweep(
                        conn,
                        tenant_id,
                        finished_at=datetime.now(UTC),
                        counts=fields | {"error": swept.error},
                    )
            except Exception:
                log.exception("screenshot sweep not recorded")
        total.add(swept)
    return total


async def _sweep_tenant(db: Database, objects: ObjectStore, tenant_id: UUID) -> Swept:
    swept = Swept()
    try:
        keys = await objects.list(f"{tenant_id}/observations/")
        swept.scanned = len(keys)
        listed: dict[UUID, str] = {}
        for key in keys:
            name = key.rsplit("/", 1)[-1].removesuffix(".png")
            with contextlib.suppress(ValueError):
                if screenshot_key(tenant_id, UUID(name)) == key:
                    listed[UUID(name)] = key
                    continue
            swept.skipped_unrecognized += 1
        if listed:
            async with db.tenant(tenant_id) as conn:
                held = await retention.screenshots_held(conn, list(listed))
            swept.skipped_held = len(held)
        else:
            held = set()
    except Exception as error:
        log.exception("screenshot sweep stopped")
        swept.error = type(error).__name__
        return swept
    for observation_id, key in listed.items():
        if observation_id in held:
            continue
        try:
            await objects.delete(key)
            swept.deleted += 1
        except Exception as error:
            swept.failed += 1
            log.warning(
                "screenshot not deleted",
                extra={"observation_id": observation_id, "error_code": type(error).__name__},
            )
    return swept
