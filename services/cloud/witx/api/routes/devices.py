"""Device pools: Gateways, the devices they report, enabling them."""

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from witx_contracts import api

from witx.api import views
from witx.api.deps import (
    Deps,
    IdempotencyKey,
)
from witx.domain.rules import Rejected
from witx.services import pool
from witx.services.identity import Actor
from witx.store import devices

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/devices")
    async def list_devices(who: CurrentActor) -> list[api.DeviceView]:
        async with d.db.tenant(who.tenant_id) as conn:
            return await views.device_views(conn)

    @router.get("/api/v1/gateways")
    async def list_gateways(who: CurrentActor) -> list[api.GatewayView]:
        """The tenant's device pools (owners only)."""
        if who.role != "owner":
            raise Rejected("owner_required", "only a tenant owner can manage device pools")
        async with d.db.tenant(who.tenant_id) as conn:
            return await views.gateway_views(conn, d.hub.connected_ids())

    @router.post("/api/v1/gateways", status_code=201)
    async def add_gateway(
        body: api.GatewayCreate, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.GatewayCreated:
        """A new device pool. Its one-time code is returned once and never again."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.GatewayCreated:
                gateway_id, code = await pool.add_gateway(conn, who, body.name, datetime.now(UTC))
                (view,) = [
                    g
                    for g in await views.gateway_views(conn, d.hub.connected_ids())
                    if g.id == gateway_id
                ]
                return api.GatewayCreated(gateway=view, code=code)

            return await d.once(conn, who, key, request, body, api.GatewayCreated, write)

    @router.post("/api/v1/gateways/{gateway_id}/{change}")
    async def change_gateway(
        gateway_id: UUID, change: pool.GatewayChange, who: CurrentActor
    ) -> api.GatewayView:
        """Disable, enable or revoke a device pool (owners only)."""
        async with d.db.tenant(who.tenant_id) as conn:
            await pool.change_gateway(conn, who, gateway_id, change, d.objects)
            (view,) = [
                g
                for g in await views.gateway_views(conn, d.hub.connected_ids())
                if g.id == gateway_id
            ]
            return view

    @router.post("/api/v1/devices/{device_id}/enable")
    async def enable_device(
        device_id: UUID, body: api.DeviceEnable, who: CurrentActor
    ) -> api.DeviceView:
        return await _set_enabled(who, device_id, enabled=True, name=body.name)

    @router.post("/api/v1/devices/{device_id}/disable")
    async def disable_device(device_id: UUID, who: CurrentActor) -> api.DeviceView:
        return await _set_enabled(who, device_id, enabled=False, name=None)

    async def _set_enabled(
        who: Actor, device_id: UUID, *, enabled: bool, name: str | None
    ) -> api.DeviceView:
        async with d.db.tenant(who.tenant_id) as conn:
            await pool.set_enabled(
                conn, who, device_id, enabled=enabled, name=name, objects=d.objects
            )
            return next(v for v in await views.device_views(conn) if v.id == device_id)

    @router.get("/api/v1/devices/{device_id}/changes")
    async def device_changes(device_id: UUID, who: CurrentActor) -> list[api.DeviceChange]:
        async with d.db.tenant(who.tenant_id) as conn:
            return [api.DeviceChange(**c) for c in await devices.changes(conn, device_id)]

    return router
