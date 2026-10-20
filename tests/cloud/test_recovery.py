"""Restoring the database from a backup : `witx admin recover` replays
what happened since the backup and stops what was in flight, so nothing deleted, revoked
or half-done comes back to life."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from witx.domain.model import ActionStatus, Effect, TaskStatus
from witx.objects import LocalObjectStore, S3ObjectStore
from witx.services import ledger, pool, recovery, retention
from witx.services.identity import Actor
from witx.store import devices, sessions
from witx.store.db import Database, fetch_one

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete, tap


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=3)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(i) for i in range(3)])
    return rig


def owner(rig: Rig) -> Actor:
    return Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", "owner")


async def finished(rig: Rig, device_index: int) -> object:
    device = rig.tenant.device_ids[device_index]
    rig.model.script = [[call("observe", device_id=device)], lambda r: tap(r, device), complete]
    task = await rig.create_task(device_ids=[device])
    await rig.settle()
    return task


async def in_flight(rig: Rig, device_index: int, effects: list[str]) -> object:
    device = rig.tenant.device_ids[device_index]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=effects),
    ]
    task = await rig.create_task(device_ids=[device])
    await rig.settle()
    return task


async def test_after_a_restore_nothing_deleted_revoked_or_half_done_comes_back(
    rig: Rig, admin_db: Database
) -> None:
    # The backup holds: a finished task, a task whose input waits for approval, a task
    # whose input was sent with no result yet, an enabled device, a session.
    done = await finished(rig, 0)
    held = await in_flight(rig, 1, ["delete_data"])
    rig.gateway.hold_actions = True
    sent = await in_flight(rig, 2, [])
    async with admin_db.system() as conn:
        await sessions.create(
            conn, user_id=rig.tenant.user_id, subject=rig.tenant.subject, now=datetime.now(UTC)
        )
        epoch_before = (await devices.get(conn, rig.tenant.device_ids[2])).epoch  # type: ignore[union-attr]

    # After the backup (lost by the restore, kept in the ledger): the finished task was
    # deleted, device 0 disabled, the Gateway disabled.
    await ledger.record(rig.objects, "task", done.id, {"deleted": True, "by": rig.tenant.user_id})  # type: ignore[attr-defined]
    state = {"enabled": False, "name": "Pixel 0", "by": rig.tenant.user_id}
    await ledger.record(rig.objects, "device", rig.tenant.device_ids[0], state)
    await ledger.record(rig.objects, "gateway", rig.tenant.gateway_id, {"state": "disabled"})

    first = await recovery.recover(admin_db, rig.objects, datetime.now(UTC))
    assert first["tasks_deleted"] == 1 and first["devices"] == 1 and first["gateways"] == 1
    assert first["tasks_stopped"] == 2 and first["inputs_cancelled"] == 1
    assert first["inputs_unknown"] == 1 and first["sessions_ended"] == 1

    async with admin_db.system() as conn:
        deleted = await fetch_one(conn, "SELECT deleted_at FROM tasks WHERE id = :t", t=done.id)  # type: ignore[attr-defined]
        assert deleted is not None and deleted["deleted_at"] is not None
        assert not (await devices.get(conn, rig.tenant.device_ids[0])).enabled  # type: ignore[union-attr]
        assert not await devices.gateway_allowed(conn, rig.tenant.gateway_id)
        assert await devices.lease(conn, rig.tenant.device_ids[1]) is None
        jumped = (await devices.get(conn, rig.tenant.device_ids[2])).epoch  # type: ignore[union-attr]
        assert jumped >= epoch_before + 1_000_000  # above anything issued after the backup
    for task, action_status in ((held, ActionStatus.CANCELLED), (sent, ActionStatus.DONE)):
        assert (await rig.task(task.id)).status == TaskStatus.STOPPED  # type: ignore[attr-defined]
        (action,) = await rig.actions(task.id)  # type: ignore[attr-defined]
        assert action.status == action_status
    (unsettled,) = await rig.actions(sent.id)  # type: ignore[attr-defined]
    assert unsettled.effect == Effect.UNKNOWN  # its device stays isolated until settled

    again = await recovery.recover(admin_db, rig.objects, datetime.now(UTC))  # idempotent
    assert again["tasks_deleted"] == again["gateways"] == again["devices"] == 0
    assert again["tasks_stopped"] == again["inputs_cancelled"] == again["inputs_unknown"] == 0


async def test_deleting_a_task_or_changing_a_device_is_written_to_the_ledger_first(
    rig: Rig,
) -> None:
    task = await finished(rig, 0)
    async with rig.db.tenant(rig.tenant.id) as conn:
        await retention.delete_task(conn, owner(rig), task.id, datetime.now(UTC), rig.objects)  # type: ignore[attr-defined]
        await pool.set_enabled(
            conn,
            owner(rig),
            rig.tenant.device_ids[1],
            enabled=False,
            name=None,
            objects=rig.objects,
        )
    entries = {(kind, entity): state for kind, entity, state in await ledger.entries(rig.objects)}
    assert entries[("task", task.id)]["deleted"] is True  # type: ignore[attr-defined]
    assert entries[("device", rig.tenant.device_ids[1])]["enabled"] is False


async def test_the_local_store_lists_keys_under_a_prefix(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    for key in ("ledger/task/a", "ledger/device/b", "screenshots/x.png"):
        await store.put(key, b"1", "text/plain")
    assert await store.list("ledger/") == ["ledger/device/b", "ledger/task/a"]


async def test_s3_lists_every_page_with_a_signed_query() -> None:
    pages = [
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Contents><Key>'
        b"ledger/task/a</Key></Contents><IsTruncated>true</IsTruncated>"
        b"<NextContinuationToken>t+1</NextContinuationToken></ListBucketResult>",
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Contents><Key>'
        b"ledger/device/b</Key></Contents><IsTruncated>false</IsTruncated></ListBucketResult>",
    ]
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=pages[len(seen) - 1])

    store = S3ObjectStore(
        endpoint="https://s3.example", bucket="b", region="r", access_key="k", secret_key="s"
    )
    store.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    assert await store.list("ledger/") == ["ledger/device/b", "ledger/task/a"]
    assert seen[0].url.params["list-type"] == "2" and seen[0].url.params["prefix"] == "ledger/"
    assert seen[1].url.params["continuation-token"] == "t+1"
    assert "Signature=" in seen[1].headers["authorization"]
