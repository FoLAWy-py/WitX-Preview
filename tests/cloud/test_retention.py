"""Deleting tasks and retention ."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from witx.api.app import create_app
from witx.api.auth import DevAuthenticator
from witx.config import Settings
from witx.domain import rules
from witx.domain.model import Effect, TaskStatus
from witx.domain.rules import Rejected
from witx.services import execution as exec_service
from witx.services import lifecycle, retention
from witx.services.identity import Actor
from witx.store import tasks
from witx.store.db import Database, fetch, fetch_one

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete, tap
from tests.conftest import Databases

TOKEN = "dev-token-acme"
SCREENSHOTS = timedelta(days=30)
AUDIT = timedelta(days=90)


@pytest.fixture
async def rig(databases: Databases, db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    return rig


@pytest.fixture
async def web(rig: Rig, databases: Databases) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=databases.app_url,
        object_store_url="file:///unused",
        dev_tokens={TOKEN: rig.tenant.subject},
    )
    app = create_app(
        settings,
        db=rig.db,
        objects=rig.objects,
        authenticator=DevAuthenticator(settings.dev_tokens),
        hub=rig.hub,
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


def owner(rig: Rig) -> Actor:
    return Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")


async def completed_task(rig: Rig, device: UUID) -> UUID:
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device),
        complete,
    ]
    task = await rig.create_task(device_ids=[device])
    await rig.settle()
    assert (await rig.task(task.id)).status == TaskStatus.COMPLETED
    return task.id


async def purge(rig: Rig, *, later: timedelta = timedelta(0)) -> retention.Purged:
    return await retention.purge(
        rig.db,
        rig.objects,
        now=datetime.now(UTC) + later,
        screenshot_retention=SCREENSHOTS,
        audit_retention=AUDIT,
    )


async def screenshots(admin_db: Database, task_id: UUID) -> list[dict[str, Any]]:
    async with admin_db.system() as conn:
        rows = await fetch(
            conn, "SELECT id, object_key FROM observations WHERE task_id = :t", t=task_id
        )
    return [dict(r) for r in rows]


async def leftovers(admin_db: Database, task_id: UUID) -> dict[str, int]:
    counts = {}
    async with admin_db.system() as conn:
        for table in ("task_inputs", "runs", "observations", "actions", "events"):
            row = await fetch_one(
                conn, f"SELECT count(*) AS n FROM {table} WHERE task_id = :t", t=task_id
            )
            assert row is not None
            counts[table] = row["n"]
    return counts


async def test_a_deleted_task_disappears_and_its_content_and_screenshots_are_purged(  # type: ignore[no-untyped-def]
    rig: Rig, web, admin_db: Database, tmp_path: Path
) -> None:
    task_id = await completed_task(rig, rig.tenant.device_ids[0])
    shots = await screenshots(admin_db, task_id)
    assert shots and all((tmp_path / s["object_key"]).exists() for s in shots)

    assert (await web.delete(f"/api/v1/tasks/{task_id}")).status_code == 204
    assert (await web.get(f"/api/v1/tasks/{task_id}")).status_code == 404
    assert (await web.get("/api/v1/tasks")).json() == []
    shot = await web.get(f"/api/v1/observations/{shots[0]['id']}/screenshot")
    assert shot.status_code == 404  # hidden before the purge has run

    assert (await purge(rig)).tasks == 1
    assert not any((tmp_path / s["object_key"]).exists() for s in shots)
    assert set((await leftovers(admin_db, task_id)).values()) == {0}
    async with admin_db.system() as conn:
        tombstone = await fetch_one(conn, "SELECT goal, audit FROM tasks WHERE id = :t", t=task_id)
    assert tombstone is not None and tombstone["goal"] == ""
    audit = tombstone["audit"]
    assert audit["actions"] == [{"kind": "tap", "effect": "unverified", "count": 1}]
    assert audit["usage"]["model_calls"] > 0 and isinstance(audit["usage"]["input_tokens"], int)
    # Deleting again is not an error.
    assert (await web.delete(f"/api/v1/tasks/{task_id}")).status_code == 204


async def test_only_a_finished_task_can_be_deleted(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [[call("ask_user", question="Which network?")]]
    task = await rig.create_task(device_ids=[device])
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected, match="task_not_finished"):
            await retention.delete_task(conn, owner(rig), task.id, datetime.now(UTC), rig.objects)


async def test_a_task_with_an_unknown_effect_cannot_be_deleted_until_reconciled(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.outcome = "unknown"
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device),
        [call("ask_user", question="Did the tap happen?")],
    ]
    task = await rig.create_task(device_ids=[device])
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.stop(conn, task.id)
    await rig.settle()
    (action,) = await rig.actions(task.id)
    assert action.effect == Effect.UNKNOWN
    assert (await rig.task(task.id)).status == TaskStatus.STOPPED
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected, match="unknown_effect_pending"):
            await retention.delete_task(conn, owner(rig), task.id, datetime.now(UTC), rig.objects)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await exec_service.reconcile(
            conn,
            action_id=action.id,
            effect="no_effect",
            observation_id=action.post_observation_id,
            user_id=rig.tenant.user_id,
        )
        await retention.delete_task(conn, owner(rig), task.id, datetime.now(UTC), rig.objects)
        assert await tasks.get(conn, task.id) is None


async def test_only_its_creator_or_a_tenant_owner_can_delete_a_task(rig: Rig) -> None:
    task_id = await completed_task(rig, rig.tenant.device_ids[0])
    someone = Actor(uuid4(), "Bob", rig.tenant.id, "acme", "member")
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected, match="owner_required"):
            await retention.delete_task(conn, someone, task_id, datetime.now(UTC), rig.objects)
        creator = Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "member")
        await retention.delete_task(conn, creator, task_id, datetime.now(UTC), rig.objects)


async def test_screenshots_expire_except_those_of_unfinished_tasks_and_unknown_evidence(  # type: ignore[no-untyped-def]
    rig: Rig, web, admin_db: Database, tmp_path: Path
) -> None:
    first, second = rig.tenant.device_ids
    done = await completed_task(rig, first)
    # An unfinished task on the other device, and one whose action has an unknown effect.
    rig.model.script = [[call("observe", device_id=second)], [call("ask_user", question="?")]]
    waiting = await rig.create_task(device_ids=[second])
    await rig.settle()
    rig.gateway.outcome = "unknown"
    rig.model.script = [[call("observe", device_id=first)], lambda r: tap(r, first)]
    unresolved = await rig.create_task(device_ids=[first])
    await rig.settle()
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.stop(conn, unresolved.id)
    await rig.settle()
    (action,) = await rig.actions(unresolved.id)

    assert (await purge(rig)).screenshots == 0  # nothing is old yet
    await purge(rig, later=SCREENSHOTS + timedelta(hours=1))

    expired = await screenshots(admin_db, done)
    assert expired and all(s["object_key"] is None for s in expired)
    gone = await web.get(f"/api/v1/observations/{expired[0]['id']}/screenshot")
    assert gone.status_code == 410 and gone.json()["code"] == "purged"
    assert all(s["object_key"] for s in await screenshots(admin_db, waiting.id))
    kept = {s["id"]: s["object_key"] for s in await screenshots(admin_db, unresolved.id)}
    assert kept[action.observation_id] and (tmp_path / kept[action.observation_id]).exists()


async def test_a_deleted_task_leaves_its_audit_summary_until_the_audit_retention_ends(
    rig: Rig, admin_db: Database
) -> None:
    task_id = await completed_task(rig, rig.tenant.device_ids[0])
    async with rig.db.tenant(rig.tenant.id) as conn:
        await retention.delete_task(conn, owner(rig), task_id, datetime.now(UTC), rig.objects)
    await purge(rig)
    assert (await purge(rig, later=AUDIT - timedelta(days=1))).tombstones == 0
    assert (await purge(rig, later=AUDIT + timedelta(days=1))).tombstones == 1
    async with admin_db.system() as conn:
        assert await fetch_one(conn, "SELECT 1 FROM tasks WHERE id = :t", t=task_id) is None


async def test_a_failed_deletion_leaves_the_task_for_the_next_purge(  # type: ignore[no-untyped-def]
    rig: Rig, web, admin_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await completed_task(rig, rig.tenant.device_ids[0])
    shots = await screenshots(admin_db, task_id)
    assert (await web.delete(f"/api/v1/tasks/{task_id}")).status_code == 204

    async def unavailable(key: str) -> None:
        raise OSError("object store unavailable")

    monkeypatch.setattr(rig.objects, "delete", unavailable)
    with pytest.raises(OSError):
        await purge(rig)
    assert set((await leftovers(admin_db, task_id)).values()) != {0}  # rows still list it
    monkeypatch.undo()
    assert (await purge(rig)).tasks == 1  # the next run finishes the job
    assert not any((tmp_path / s["object_key"]).exists() for s in shots)
    assert set((await leftovers(admin_db, task_id)).values()) == {0}


async def test_a_person_who_keeps_screenshots_fewer_days_has_them_deleted_sooner(  # type: ignore[no-untyped-def]
    rig: Rig, web, admin_db: Database
) -> None:
    chosen = await web.put("/api/v1/me/retention", json={"screenshot_retention_days": 3})
    assert chosen.status_code == 200
    me = (await web.get("/api/v1/me")).json()
    assert (me["screenshot_retention_days"], me["screenshot_retention_max"]) == (3, 30)
    done = await completed_task(rig, rig.tenant.device_ids[0])

    await purge(rig, later=timedelta(days=2))
    assert all(s["object_key"] for s in await screenshots(admin_db, done))
    await purge(rig, later=timedelta(days=3, hours=1))
    assert all(s["object_key"] is None for s in await screenshots(admin_db, done))


async def test_no_one_keeps_screenshots_longer_than_thirty_days_or_the_deployment(  # type: ignore[no-untyped-def]
    web,
) -> None:
    longer = await web.put("/api/v1/me/retention", json={"screenshot_retention_days": 31})
    assert longer.status_code == 422
    assert rules.retention_most(90) == 30
    assert rules.retention_most(7) == 7
