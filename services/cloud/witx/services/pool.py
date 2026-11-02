"""Enabling devices (device pool). A Gateway drives every device it finds; a device
takes tasks only after an owner enabled it here. Every change is recorded with who made
it (``device_changes``)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.model import Device
from witx.domain.rules import Rejected
from witx.objects import ObjectStore
from witx.services import identity as identity_service
from witx.services import ledger
from witx.services.identity import Actor
from witx.store import devices, execution, identity
from witx.store.db import Database

MAX_NAME = 128


async def set_enabled(
    conn: AsyncConnection,
    actor: Actor,
    device_id: UUID,
    *,
    enabled: bool,
    name: str | None,
    objects: ObjectStore,
) -> Device:
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can enable or disable devices")
    device = await devices.get(conn, device_id, lock=True)
    if device is None:
        raise Rejected("not_found")
    label = (name or "").strip()[:MAX_NAME] or device.name
    # In the restore ledger first: an older backup must not undo this.
    state = {"enabled": enabled, "name": label, "by": actor.user_id}
    await ledger.record(objects, "device", device_id, state)
    await devices.set_enabled(conn, device_id, user_id=actor.user_id, enabled=enabled, name=label)
    updated = await devices.get(conn, device_id)
    assert updated is not None
    return updated


async def remove(
    conn: AsyncConnection, actor: Actor, device_id: UUID, *, objects: ObjectStore
) -> None:
    """Removes a device that is gone: disabled and no longer listed, its row kept
    for the history of the tasks that used it."""
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can remove devices")
    device = await devices.get(conn, device_id, lock=True)
    if device is None or await devices.removed(conn, device_id):
        raise Rejected("not_found")
    state = await execution.device_state(conn, device_id)
    rules.check_device_removable(
        online=device.online,
        problems=device.problems,
        held=await devices.lease(conn, device_id) is not None,
        unknown_pending=state["unknown"],
    )
    # In the restore ledger first: an older backup must not bring it back.
    record = {"enabled": False, "name": device.name, "by": actor.user_id, "removed": True}
    await ledger.record(objects, "device", device_id, record)
    await devices.remove(conn, device_id, user_id=actor.user_id)


GatewayChange = Literal["disable", "enable", "revoke"]


async def take_silent_offline(db: Database, now: datetime) -> int:
    """Takes offline the devices of every Gateway not heard from for GATEWAY_SILENCE,
    tenant by tenant under each one's row-level security. Several workers may do
    this at once: it only sets ``online`` to false. Returns how many went offline."""
    async with db.system() as conn:
        tenants = [row["id"] for row in await identity.tenants(conn)]
    gone = 0
    for tenant_id in tenants:
        async with db.tenant(tenant_id) as conn:
            gone += await devices.set_silent_gateways_offline(conn, rules.gateway_silent_since(now))
    return gone


def _owner(actor: Actor) -> None:
    if actor.role != "owner":
        raise Rejected("owner_required", "only a tenant owner can manage device pools")


async def add_gateway(
    conn: AsyncConnection, actor: Actor, name: str, now: datetime
) -> tuple[UUID, str]:
    """A new device pool waiting for its Gateway to enroll: its ID and the one-time code."""
    _owner(actor)
    label = " ".join(name.split())[:MAX_NAME]
    if not label:
        raise Rejected("invalid_arguments", "a device pool needs a name")
    return await identity_service.create_gateway(
        conn, tenant_id=actor.tenant_id, name=label, now=now
    )


async def change_gateway(
    conn: AsyncConnection,
    actor: Actor,
    gateway_id: UUID,
    change: GatewayChange,
    objects: ObjectStore,
) -> None:
    """Disables a Gateway (until enabled again) or revokes it for good. A connected one
    is cut off at its next heartbeat."""
    _owner(actor)
    if not await devices.gateway_known(conn, gateway_id):
        raise Rejected("not_found", "unknown or revoked device pool")
    # In the restore ledger first: an older backup must not undo this.
    state = {"enable": "enabled", "disable": "disabled", "revoke": "revoked"}[change]
    await ledger.record(objects, "gateway", gateway_id, {"state": state, "by": actor.user_id})
    if change == "revoke":
        await devices.revoke_gateway(conn, gateway_id)
    else:
        await devices.set_gateway_disabled(conn, gateway_id, change == "disable")
