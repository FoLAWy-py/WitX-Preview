"""The device pool: the cloud keeps what each Gateway reports about its devices; a
device takes tasks only once an owner enabled it, and only while nothing is missing."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from witx.api import views
from witx.domain import rules
from witx.domain.rules import Rejected
from witx.services import pool
from witx.services.identity import Actor
from witx.store import devices
from witx.store.db import Database
from witx_contracts import api
from witx_contracts import gateway as wire

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision

NEW = {"native_id": "NEW1", "platform": "android", "name": "Pixel 9", "capabilities": ["tap"]}


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def report(rig: Rig, *reported: dict[str, object]) -> dict[str, api.DeviceView]:
    await rig.handle(wire.Devices(devices=[wire.DeviceInfo(**d) for d in reported]))  # type: ignore[arg-type]
    async with rig.db.tenant(rig.tenant.id) as conn:
        return {v.name: v for v in await views.device_views(conn)}


def owner(rig: Rig, role: str = "owner") -> Actor:
    return Actor(rig.tenant.user_id, "Acme", rig.tenant.id, "acme", role)


async def test_a_newly_plugged_in_device_waits_for_an_owner_to_enable_it(rig: Rig) -> None:
    shown = await report(rig, device_info(0), NEW)
    new = shown["Pixel 9"]
    assert not new.enabled and new.pool == shown["Pixel 0"].pool != ""
    with pytest.raises(Rejected, match="device_not_enabled"):
        await rig.create_task(device_ids=[new.id])
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected, match="owner_required"):
            await pool.set_enabled(
                conn, owner(rig, "member"), new.id, enabled=True, name=None, objects=rig.objects
            )
        await pool.set_enabled(
            conn, owner(rig), new.id, enabled=True, name="Test Pixel", objects=rig.objects
        )
    await rig.create_task(device_ids=[new.id])
    shown = await report(rig, device_info(0), NEW)  # the Gateway's name no longer applies
    assert shown["Test Pixel"].enabled


async def test_every_enabling_and_disabling_is_recorded_with_who_did_it(rig: Rig) -> None:
    device_id = rig.tenant.device_ids[0]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await pool.set_enabled(
            conn, owner(rig), device_id, enabled=False, name=None, objects=rig.objects
        )
        assert not (await devices.get(conn, device_id)).enabled  # type: ignore[union-attr]
        history = await devices.changes(conn, device_id)
    assert [(c["change"], c["by"]) for c in history] == [("disabled", "Acme"), ("enabled", "Acme")]
    with pytest.raises(Rejected, match="device_not_enabled"):
        await rig.create_task()


async def test_an_unplugged_device_is_not_ready_until_it_is_back(rig: Rig) -> None:
    shown = await report(rig, {**device_info(0), "problems": ["disconnected"]})
    assert shown["Pixel 0"].problems == ["disconnected"]
    with pytest.raises(Rejected, match="device_not_ready"):
        await rig.create_task()
    shown = await report(rig, device_info(0))
    assert shown["Pixel 0"].problems == []
    await rig.create_task()


async def test_the_gateway_learns_the_id_of_a_device_plugged_in_later(rig: Rig) -> None:
    rig.sent.clear()
    shown = await report(rig, device_info(0), NEW)
    (bindings,) = [wire.Bindings.model_validate_json(m) for m in rig.sent if '"bindings"' in m]
    assert {(b.native_id, b.device_id) for b in bindings.devices} >= {("NEW1", shown["Pixel 9"].id)}


async def test_a_device_that_is_gone_can_be_removed_and_comes_back_disabled_if_seen_again(
    rig: Rig,
) -> None:
    device_id = rig.tenant.device_ids[0]
    await report(rig, NEW)  # the Gateway no longer lists Pixel 0
    async with rig.db.tenant(rig.tenant.id) as conn:
        await pool.remove(conn, owner(rig), device_id, objects=rig.objects)
        history = await devices.changes(conn, device_id)
        (pool_view,) = await views.gateway_views(conn, set())
    assert history[0]["change"] == "removed" and history[0]["by"] == "Acme"
    assert "Pixel 0" not in await report(rig, NEW)
    assert pool_view.devices == 1  # the pool counts Pixel 9 alone

    back = await report(rig, device_info(0), NEW)
    assert not back["Pixel 0"].enabled  # an owner enables it again, as a new device


async def test_a_removed_device_its_gateway_still_cannot_find_stays_removed(rig: Rig) -> None:
    device_id = rig.tenant.device_ids[0]
    await report(rig, {**device_info(0), "problems": ["disconnected"]})
    async with rig.db.tenant(rig.tenant.id) as conn:
        await pool.remove(conn, owner(rig), device_id, objects=rig.objects)
    # A Gateway reports the devices of its configuration it cannot find.
    assert "Pixel 0" not in await report(rig, {**device_info(0), "problems": ["disconnected"]}, NEW)


async def test_the_devices_of_a_gateway_no_process_has_heard_from_go_offline(rig: Rig) -> None:
    device_id = rig.tenant.device_ids[0]
    now = datetime.now(UTC)
    assert await pool.take_silent_offline(rig.db, now) == 0  # its process says it is there
    async with rig.db.tenant(rig.tenant.id) as conn:
        assert (await devices.get(conn, device_id)).online  # type: ignore[union-attr]

    # The process serving it was killed: nothing marked its devices offline.
    later = now + rules.GATEWAY_SILENCE + timedelta(seconds=1)
    assert await pool.take_silent_offline(rig.db, later) == 1
    async with rig.db.tenant(rig.tenant.id) as conn:
        gone = await devices.get(conn, device_id)
        (seen,) = [g["last_seen_at"] for g in await devices.list_gateways(conn)]
    assert gone is not None and not rules.usable(gone) and gone.last_seen_at == seen


async def test_a_device_that_is_connected_or_held_is_not_removed(rig: Rig) -> None:
    device_id = rig.tenant.device_ids[0]
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected, match="device_connected"):
            await pool.remove(conn, owner(rig), device_id, objects=rig.objects)
    rig.model.script = [
        [call("observe", device_id=str(device_id))],
        [call("ask_user", question="Which one?", options=[])],
    ]
    await rig.create_task()
    await rig.settle()
    await report(rig, NEW)  # unplugged while the task holds it
    async with rig.db.tenant(rig.tenant.id) as conn:
        with pytest.raises(Rejected, match="owner_required"):
            await pool.remove(conn, owner(rig, "member"), device_id, objects=rig.objects)
        with pytest.raises(Rejected, match="device_in_use"):
            await pool.remove(conn, owner(rig), device_id, objects=rig.objects)
