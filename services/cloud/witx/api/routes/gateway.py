"""The Gateway API: enrollment, credentials, screenshot uploads and the connection."""

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from witx_contracts import gateway as wire

from witx.api.deps import (
    Deps,
    bearer,
)
from witx.domain.model import ObservationStatus
from witx.hub import MAX_SCREENSHOT_BYTES, GatewayRemoved, png_size, screenshot_key
from witx.services import identity
from witx.store import devices, execution

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()

    async def gateway(authorization: Annotated[str | None, Header()] = None) -> tuple[UUID, UUID]:
        async with d.db.system() as conn:
            found = await identity.gateway_for_token(conn, bearer(authorization))
        if found is None:
            raise HTTPException(401, "invalid gateway token")
        return found

    @router.post("/gateway/v1/enroll", response_model=wire.Credential)
    async def enroll(body: wire.Enrollment) -> wire.Credential:
        """A new Gateway spends its one-time code for its first credential ."""
        gateway_id, credential = await identity.enroll_gateway(
            d.db, code=body.code, host=body.host, now=datetime.now(UTC)
        )
        return wire.Credential(gateway_id=gateway_id, credential=credential)

    @router.post("/gateway/v1/credential", response_model=wire.Credential)
    async def rotate(auth: Annotated[tuple[UUID, UUID], Depends(gateway)]) -> wire.Credential:
        """The Gateway's next credential; the current one works until this one is used."""
        gateway_id, tenant_id = auth
        async with d.db.tenant(tenant_id) as conn:
            credential = await identity.rotate_gateway_credential(conn, gateway_id)
        return wire.Credential(gateway_id=gateway_id, credential=credential)

    @router.put("/gateway/v1/observations/{observation_id}/screenshot", status_code=204)
    async def upload(
        observation_id: UUID,
        request: Request,
        auth: Annotated[tuple[UUID, UUID], Depends(gateway)],
    ) -> None:
        gateway_id, tenant_id = auth
        async with d.db.tenant(tenant_id) as conn:
            obs = await execution.get_observation(conn, observation_id)
            device = await devices.get(conn, obs.device_id) if obs else None
        if obs is None or device is None or device.gateway_id != gateway_id:
            raise HTTPException(404, "observation not found")
        if obs.status != ObservationStatus.SENT:
            raise HTTPException(409, "observation is not open")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise HTTPException(413, "screenshot too large")
        if png_size(bytes(data)) is None:
            raise HTTPException(415, "expected a PNG image")
        await d.objects.put(screenshot_key(tenant_id, observation_id), bytes(data), "image/png")

    @router.websocket("/gateway/v1/connect")
    async def connect(websocket: WebSocket) -> None:
        token = websocket.headers.get("authorization")
        try:
            async with d.db.system() as conn:
                found = await identity.gateway_for_token(conn, bearer(token))
        except HTTPException:
            found = None
        if found is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        gateway_id, tenant_id = found
        try:
            await d.hub.serve(gateway_id, tenant_id, websocket.receive_text, websocket.send_text)
        except WebSocketDisconnect:
            pass
        except GatewayRemoved:
            await websocket.close(code=4401)

    return router
