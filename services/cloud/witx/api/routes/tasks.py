"""Tasks: creating, listing, steering and deleting them, their events and screenshots."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from witx_contracts import api

from witx.api import views
from witx.api.deps import (
    Deps,
    IdempotencyKey,
)
from witx.domain import rules
from witx.domain.model import ObservationStatus, PermissionMode, Saying
from witx.domain.rules import Rejected
from witx.services import lifecycle, retention, thumbnails
from witx.services.identity import Actor
from witx.store import execution, runs, tasks

log = logging.getLogger(__name__)


def saying_frames(before: dict[UUID, Saying], now: dict[UUID, Saying]) -> list[bytes]:
    """The ``agent.saying`` frames for what changed in what the runs say or are at. They carry no
        ``id``: not events, not kept, not resent after a reconnect; an
    empty text with no phase says a run's step is over."""
    over = Saying("", None)
    changed = [(run_id, over) for run_id in before.keys() - now.keys()]
    changed += [(run_id, s) for run_id, s in now.items() if before.get(run_id) != s]
    frames = []
    for run_id, s in changed:
        view = api.SayingView(run_id=run_id, text=s.text, phase=s.phase)
        frames.append(f"event: agent.saying\ndata: {view.model_dump_json()}\n\n".encode())
    return frames


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/tasks")
    async def list_tasks(
        who: CurrentActor,
        group: str | None = None,
        attention: bool = False,
        q: str | None = Query(None, max_length=200),
        limit: int = Query(100, ge=1, le=500),
        pinned: Literal["only", "without"] | None = None,
    ) -> list[api.TaskSummary]:
        """Recent tasks, newest change first. ``group`` is a group's ID, or ``none`` for
        tasks in no group; ``attention`` keeps only tasks that wait for someone; ``q``
        only goals containing that text; ``pinned`` only the tasks the signed-in person
        pinned (latest pinned first), or none of them."""
        if group not in (None, tasks.UNGROUPED):
            try:
                UUID(str(group))
            except ValueError:
                raise Rejected("invalid_arguments", "group is a group ID or none") from None
        async with d.db.tenant(who.tenant_id) as conn:
            found = await tasks.list_with_attention(
                conn,
                limit,
                group=group,
                attention_only=attention,
                search=(q or "").strip(),
                pins=(who.user_id, pinned) if pinned else None,
            )
            mine = await tasks.pinned(conn, who.user_id, [t.id for t, _ in found])
            return [views.summary(t, needs, pinned=t.id in mine) for t, needs in found]

    @router.get("/api/v1/task-groups")
    async def list_task_groups(who: CurrentActor) -> list[api.TaskGroupView]:
        async with d.db.tenant(who.tenant_id) as conn:
            return [views.group(g) for g in await tasks.list_groups(conn)]

    @router.post("/api/v1/task-groups", status_code=201)
    async def create_task_group(
        body: api.TaskGroupName, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TaskGroupView:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskGroupView:
                group = await tasks.create_group(
                    conn,
                    tenant_id=who.tenant_id,
                    user_id=who.user_id,
                    name=rules.group_name(body.name),
                )
                return views.group(group)

            return await d.once(conn, who, key, request, body, api.TaskGroupView, write)

    @router.patch("/api/v1/task-groups/{group_id}")
    async def rename_task_group(
        group_id: UUID, body: api.TaskGroupName, who: CurrentActor
    ) -> api.TaskGroupView:
        async with d.db.tenant(who.tenant_id) as conn:
            group = await tasks.rename_group(conn, group_id, rules.group_name(body.name))
            if group is None:
                raise Rejected("not_found", "group not found")
            return views.group(group)

    @router.delete("/api/v1/task-groups/{group_id}", status_code=204)
    async def delete_task_group(group_id: UUID, who: CurrentActor) -> None:
        """Its tasks stay, in no group. Deleting again is not an error."""
        async with d.db.tenant(who.tenant_id) as conn:
            await tasks.delete_group(conn, group_id)

    @router.put("/api/v1/tasks/{task_id}/group")
    async def move_task(
        task_id: UUID, body: api.TaskGroupMove, who: CurrentActor
    ) -> api.TaskSummary:
        async with d.db.tenant(who.tenant_id) as conn:
            if body.group_id is not None and await tasks.get_group(conn, body.group_id) is None:
                raise Rejected("not_found", "group not found")
            if not await tasks.set_group(conn, task_id, body.group_id):
                raise Rejected("not_found", "task not found")
            (moved,) = await tasks.list_with_attention(conn, 1, task_id=task_id)
            mine = await tasks.pinned(conn, who.user_id, [task_id])
            return views.summary(*moved, pinned=task_id in mine)

    @router.put("/api/v1/tasks/{task_id}/pin", status_code=204)
    async def pin_task(task_id: UUID, who: CurrentActor) -> None:
        """Pinned for the signed-in person only; pinning again is not an error."""
        async with d.db.tenant(who.tenant_id) as conn:
            if not await tasks.pin(conn, who.tenant_id, who.user_id, task_id):
                raise Rejected("not_found", "task not found")

    @router.delete("/api/v1/tasks/{task_id}/pin", status_code=204)
    async def unpin_task(task_id: UUID, who: CurrentActor) -> None:
        async with d.db.tenant(who.tenant_id) as conn:
            await tasks.unpin(conn, who.user_id, task_id)

    @router.post("/api/v1/tasks", status_code=201)
    async def create_task(
        body: api.TaskCreate, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                effort = body.reasoning_effort or d.settings.reasoning_effort
                model = body.model or d.settings.default_model
                await d.check_model(conn, who.tenant_id, model, effort)
                task = await lifecycle.create_task(
                    conn,
                    tenant_id=who.tenant_id,
                    user_id=who.user_id,
                    request=body.goal,
                    constraints=body.constraints,
                    device_ids=list(dict.fromkeys(body.device_ids)),
                    model=model,
                    permission_mode=PermissionMode(body.permission_mode),
                    reasoning_effort=effort,
                    timezone=body.timezone,
                    group_id=body.group_id,
                )
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: UUID, who: CurrentActor) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:
            task = await tasks.get(conn, task_id)
            if task is None:
                raise HTTPException(404, "task not found")
            return await views.task_detail(conn, task)

    @router.post("/api/v1/tasks/{task_id}/inputs")
    async def add_input(
        task_id: UUID,
        body: api.TaskInputCreate,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                task = await lifecycle.add_input(
                    conn,
                    task_id=task_id,
                    user_id=who.user_id,
                    text=body.text,
                    run_id=body.run_id,
                )
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.delete("/api/v1/tasks/{task_id}/inputs/{seq}")
    async def withdraw_input(task_id: UUID, seq: int, who: CurrentActor) -> api.TaskDetail:
        """Withdraws the person's own message while the main agent has not read it."""
        async with d.db.tenant(who.tenant_id) as conn:
            task = await lifecycle.withdraw_input(
                conn, task_id=task_id, seq=seq, user_id=who.user_id
            )
            return await views.task_detail(conn, task)

    @router.post("/api/v1/tasks/{task_id}/steer")
    async def steer_task(
        task_id: UUID, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TaskDetail:
        """The main agent reads the queued messages now."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                return await views.task_detail(conn, await lifecycle.steer(conn, task_id=task_id))

            return await d.once(conn, who, key, request, None, api.TaskDetail, write)

    @router.put("/api/v1/tasks/{task_id}/devices")
    async def task_devices(
        task_id: UUID,
        body: api.DevicesUpdate,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                task = await lifecycle.set_devices(
                    conn, task_id=task_id, user_id=who.user_id, device_ids=body.device_ids
                )
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.put("/api/v1/tasks/{task_id}/reasoning-effort")
    async def task_reasoning_effort(
        task_id: UUID,
        body: api.ReasoningEffortUpdate,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                task = await lifecycle.set_reasoning_effort(
                    conn,
                    task_id=task_id,
                    effort=body.reasoning_effort,
                    catalog=d.settings.catalog(),
                )
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.put("/api/v1/tasks/{task_id}/model")
    async def task_model(
        task_id: UUID,
        body: api.ModelUpdate,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                await d.check_model(conn, who.tenant_id, body.model)
                task = await lifecycle.set_model(
                    conn, task_id=task_id, model=body.model, catalog=d.settings.catalog()
                )
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.put("/api/v1/tasks/{task_id}/permission-mode")
    async def permission_mode(
        task_id: UUID,
        body: api.PermissionModeUpdate,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                task = await lifecycle.set_permission_mode(
                    conn,
                    task_id=task_id,
                    mode=PermissionMode(body.permission_mode),
                    limits=d.settings.limits,
                    now=datetime.now(UTC),
                )
                return await views.task_detail(conn, task)

            return await d.once(conn, who, key, request, body, api.TaskDetail, write)

    @router.post("/api/v1/tasks/{task_id}/pause")
    async def pause_task(
        task_id: UUID, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                return await views.task_detail(conn, await lifecycle.pause(conn, task_id))

            return await d.once(conn, who, key, request, None, api.TaskDetail, write)

    @router.post("/api/v1/tasks/{task_id}/resume")
    async def resume_task(
        task_id: UUID, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                return await views.task_detail(conn, await lifecycle.resume(conn, task_id))

            return await d.once(conn, who, key, request, None, api.TaskDetail, write)

    @router.post("/api/v1/tasks/{task_id}/stop")
    async def stop_task(
        task_id: UUID, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.TaskDetail:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.TaskDetail:
                return await views.task_detail(conn, await lifecycle.stop(conn, task_id))

            return await d.once(conn, who, key, request, None, api.TaskDetail, write)

    @router.delete("/api/v1/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: UUID, who: CurrentActor) -> None:
        """Deletes a finished task . It disappears at once; its screenshots
        and content are purged shortly after. Deleting again is not an error."""
        async with d.db.tenant(who.tenant_id) as conn:
            await retention.delete_task(conn, who, task_id, datetime.now(UTC), d.objects)

    @router.get("/api/v1/observations/{observation_id}/screenshot")
    async def screenshot(
        observation_id: UUID,
        who: CurrentActor,
        width: Annotated[int | None, Query(ge=32, le=1200)] = None,
    ) -> Response:
        """The screenshot as stored (PNG), or with ``width`` a JPEG that wide for a
        thumbnail (never wider than the screenshot)."""
        async with d.db.tenant(who.tenant_id) as conn:
            obs = await execution.get_observation(conn, observation_id)
            hidden = (
                obs is not None
                and obs.task_id is not None
                and (
                    await tasks.get(conn, obs.task_id) is None  # deleted, not purged yet
                )
            )
        if obs is None or obs.status != ObservationStatus.COMPLETE or hidden:
            raise HTTPException(404, "screenshot not found")
        if obs.object_key is None:
            return JSONResponse(
                {"code": "purged", "detail": "deleted under the retention policy"},
                status_code=410,
            )
        data = await d.objects.get(obs.object_key)
        cache = {"Cache-Control": "private, max-age=86400"}
        if width is not None:
            try:
                small = await asyncio.to_thread(thumbnails.thumbnail, data, width)
            except thumbnails.NotAThumbnail:
                pass  # the stored picture as it is
            else:
                return Response(small, media_type="image/jpeg", headers=cache)
        return Response(data, media_type="image/png", headers=cache)

    @router.get("/api/v1/tasks/{task_id}/events")
    async def events(
        task_id: UUID,
        who: CurrentActor,
        after: int = 0,
        last_event_id: Annotated[int | None, Header()] = None,
    ) -> StreamingResponse:
        """Server-sent events, in order and without gaps. Resume with `Last-Event-ID`."""
        async with d.db.tenant(who.tenant_id) as conn:
            if await tasks.get(conn, task_id) is None:
                raise HTTPException(404, "task not found")
        cursor = last_event_id if last_event_id is not None else after

        async def stream() -> AsyncIterator[bytes]:
            nonlocal cursor
            idle = 0.0
            saying: dict[UUID, Saying] = {}  # what each run was last heard saying
            while True:
                async with d.db.tenant(who.tenant_id) as conn:
                    rows = await tasks.events_after(conn, task_id, cursor)
                    now_saying = await runs.saying_of_task(conn, task_id)
                for words in saying_frames(saying, now_saying):
                    yield words
                saying = now_saying
                for row in rows:
                    event = api.EventView(
                        seq=row["seq"],
                        type=row["type"],
                        data=row["data"],
                        created_at=row["created_at"],
                    )
                    cursor = event.seq
                    frame = f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}"
                    yield (frame + "\n\n").encode()
                if rows:
                    idle = 0.0
                    continue
                await asyncio.sleep(0.5)
                idle += 0.5
                if idle >= 15:
                    idle = 0.0
                    yield b": keep-alive\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    # ------------------------------------------------------------ Gateway API

    return router
