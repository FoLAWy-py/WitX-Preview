"""The device pool: the cloud keeps what each Gateway reports about its devices; a
device takes tasks only once an owner enabled it, and only while nothing is missing."""

from pathlib import Path

import pytest
from witx.api import views
from witx.domain.rules import Rejected
from witx.services import pool
from witx.services.identity import Actor
from witx.store import devices
from witx.store.db import Database
from witx_contracts import api
from witx_contracts import gateway as wire

from tests.cloud.rig import Rig
from tests.cloud.support import device_info, provision

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
