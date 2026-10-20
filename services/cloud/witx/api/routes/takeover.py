"""Taking over a phone by hand."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Request,
    WebSocket,
)
from starlette.websockets import WebSocketDisconnect
from witx_contracts import api

from witx.api import takeover as takeover_socket
from witx.api import views
from witx.api.deps import (
    SESSION_COOKIE,
    Deps,
    IdempotencyKey,
)
from witx.domain.rules import Rejected
from witx.services import identity, takeover
from witx.services.identity import Actor
from witx.store import devices

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/tasks/{task_id}/devices/{device_id}/live")
    async def live_frame(task_id: UUID, device_id: UUID, who: CurrentActor) -> api.LiveFrame:
        """The screen of a device the task holds, for a person watching the task."""
        return await takeover_socket.live_frame(
            d.db, d.hub, who=who, task_id=task_id, device_id=device_id
        )

    @router.post("/api/v1/tasks/{task_id}/takeover", status_code=201)
    async def start_takeover(
        task_id: UUID,
        body: api.TakeoverStart,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TakeoverView:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TakeoverView:
                lease = await takeover.start(
                    conn,
                    task_id=task_id,
                    device_id=body.device_id,
                    user_id=who.user_id,
                    now=datetime.now(UTC),
                )
                assert lease.takeover_id and lease.takeover_expires_at
                return api.TakeoverView(
                    takeover_id=lease.takeover_id,
                    device_id=lease.device_id,
                    expires_at=lease.takeover_expires_at,
                )

            return await d.once(conn, who, key, request, body, api.TakeoverView, write)

    @router.post("/api/v1/devices/{device_id}/takeover", status_code=201)
    async def start_device_takeover(
        device_id: UUID, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TakeoverView:
        """Takes over a phone no task holds, from the device pool."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TakeoverView:
                lease = await takeover.start_on_device(
                    conn, device_id=device_id, user_id=who.user_id, now=datetime.now(UTC)
                )
                assert lease.takeover_id and lease.takeover_expires_at
                return api.TakeoverView(
                    takeover_id=lease.takeover_id,
                    device_id=lease.device_id,
                    expires_at=lease.takeover_expires_at,
                )

            return await d.once(conn, who, key, request, None, api.TakeoverView, write)

    @router.post("/api/v1/takeovers/{takeover_id}/end")
    async def end_takeover(
        takeover_id: UUID,
        body: api.TakeoverEnd,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TaskDetail | None:
        """Returns the task, or nothing for a phone taken over outside a task."""
        async with d.db.tenant(who.tenant_id) as conn:
            held = await devices.lease_by_takeover(conn, takeover_id)
            if held is not None and held.task_id is None and held.takeover_user == who.user_id:
                await takeover.end(conn, takeover_id=takeover_id, reason="ended", inputs=0)
                return None

            async def write() -> api.TaskDetail:
                lease = await devices.lease_by_takeover(conn, takeover_id)
                if lease is None or lease.takeover_user != who.user_id:
                    raise Rejected("not_found")
                task = await takeover.end(
                    conn, takeover_id=takeover_id, reason="ended", inputs=0, resume=body.resume
                )
                assert task is not None
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.websocket("/api/v1/takeovers/{takeover_id}/connect")
    async def takeover_connect(websocket: WebSocket, takeover_id: UUID) -> None:
        """A browser is let in by its session cookie, and only from one of our own pages
        (Origin); a program sends a bearer token in its first message."""
        await websocket.accept()
        origin = websocket.headers.get("origin")
        try:
            if origin is not None:
                token = websocket.cookies.get(SESSION_COOKIE)
                if origin not in d.allowed_origins or token is None:
                    raise PermissionError(origin)
                subject = await d.session_subject(token, "GET", origin, None)
                tenant = websocket.query_params.get("tenant_id")
                tenant_id = UUID(tenant) if tenant else None
            else:
                auth = api.TakeoverAuth.model_validate_json(
                    await asyncio.wait_for(websocket.receive_text(), 10)
                )
                subject = await d.bearer_subject(f"Bearer {auth.token}")
                tenant_id = auth.tenant_id
            async with d.db.system() as conn:
                who = await identity.actor_for(conn, subject, tenant_id)
        except Exception:
            await websocket.close(code=4401)
            return
        with contextlib.suppress(WebSocketDisconnect):
            await takeover_socket.serve(
                d.db,
                d.hub,
                takeover_id=takeover_id,
                who=who,
                receive=websocket.receive_text,
                send=websocket.send_text,
            )
        with contextlib.suppress(Exception):
            await websocket.close()

    return router
