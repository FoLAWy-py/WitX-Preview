"""Database invariants: tenant isolation, run claiming and event ordering."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from witx.domain.model import PermissionMode
from witx.services import lifecycle
from witx.store import devices, runs, tasks
from witx.store.db import Database, fetch

from tests.cloud.support import Tenant, provision


async def test_tenants_cannot_see_each_other(db: Database, admin_db: Database) -> None:
    a = await provision(admin_db, "a")
    b = await provision(admin_db, "b")
    async with db.tenant(a.id) as conn:
        task = await lifecycle.create_task(
            conn,
            tenant_id=a.id,
            user_id=a.user_id,
            request="g",
            constraints=[],
            device_ids=[],
            model="m",
        )
    async with db.tenant(b.id) as conn:
        assert await tasks.get(conn, task.id) is None
        assert await tasks.list_with_attention(conn) == []
        assert await runs.for_task(conn, task.id) == []
    async with db.system() as conn:
        assert await fetch(conn, "SELECT id FROM tasks") == []


async def test_rows_cannot_be_written_into_another_tenant(db: Database, admin_db: Database) -> None:
    a = await provision(admin_db, "a")
    b = await provision(admin_db, "b")
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db.tenant(a.id) as conn:
            await tasks.insert(
                conn,
                id=uuid4(),
                tenant_id=b.id,
                created_by=b.user_id,
                goal="x",
                constraints=[],
                model="m",
                device_ids=[],
                permission_mode=PermissionMode.AUTO,
            )


async def test_a_run_is_claimed_once_and_the_claim_fences_stale_workers(
    db: Database, admin_db: Database
) -> None:
    t = await provision(admin_db, "a")
    async with db.tenant(t.id) as conn:
        task = await lifecycle.create_task(
            conn,
            tenant_id=t.id,
            user_id=t.user_id,
            request="g",
            constraints=[],
            device_ids=[],
            model="m",
        )
        (run,) = await runs.for_task(conn, task.id)
    async with db.system() as conn:
        first = await runs.claim(conn, "w1", 60, 10)
        second = await runs.claim(conn, "w2", 60, 10)
    assert [c[0] for c in first] == [run.id]
    assert second == []
    epoch = first[0][2]
    async with db.tenant(t.id) as conn:
        # A worker holding an older epoch cannot change the run.
        assert not await runs.set_state(conn, run.id, runs.RunStatus.COMPLETED, epoch=epoch - 1)
        assert await runs.set_state(conn, run.id, runs.RunStatus.RUNNABLE, epoch=epoch)


async def test_events_are_numbered_without_gaps(db: Database, admin_db: Database) -> None:
    t = await provision(admin_db, "a")
    async with db.tenant(t.id) as conn:
        task = await lifecycle.create_task(
            conn,
            tenant_id=t.id,
            user_id=t.user_id,
            request="g",
            constraints=[],
            device_ids=[],
            model="m",
        )
        await tasks.emit(conn, task, "custom", n=1)
        await tasks.emit(conn, task, "custom", n=2)
        rows = await tasks.events_after(conn, task.id, 0)
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["type"] for r in rows] == ["task.created", "custom", "custom"]


async def test_device_epoch_only_increases(db: Database, admin_db: Database) -> None:
    t: Tenant = await provision(admin_db, "a", devices=1)
    device_id = t.device_ids[0]
    async with db.tenant(t.id) as conn:
        task = await lifecycle.create_task(
            conn,
            tenant_id=t.id,
            user_id=t.user_id,
            request="g",
            constraints=[],
            device_ids=[device_id],
            model="m",
        )
        (run,) = await runs.for_task(conn, task.id)
        first = await devices.grant_lease(
            conn, tenant_id=t.id, device_id=device_id, task_id=task.id, run_id=run.id
        )
        await devices.release_lease(conn, device_id)
        second = await devices.grant_lease(
            conn, tenant_id=t.id, device_id=device_id, task_id=task.id, run_id=run.id
        )
    assert second.epoch > first.epoch


async def test_only_one_process_at_a_time_does_work_that_must_not_be_done_twice(
    db: Database,
) -> None:
    async with db.only_one("titles") as first, db.only_one("titles") as second:
        assert first and not second
    async with db.only_one("titles") as again:
        assert again  # released when the first holder finished


async def test_every_table_with_a_tenant_column_is_isolated_by_row_level_security(
    admin_db: Database,
) -> None:
    async with admin_db.system() as conn:
        rows = await fetch(
            conn,
            """SELECT c.relname AS name, c.relrowsecurity AS rls,
                      EXISTS (SELECT 1 FROM pg_policies p
                               WHERE p.tablename = c.relname AND p.schemaname = 'public')
                        AS policy
                 FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                 JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND a.attname = 'tenant_id' AND NOT a.attisdropped""",
        )
    tables = {r["name"]: r["rls"] and r["policy"] for r in rows}
    # Identity, read before a tenant is known (signing in); the app may only read it.
    assert tables.pop("memberships") is False
    assert tables and all(tables.values()), [t for t, ok in tables.items() if not ok]
