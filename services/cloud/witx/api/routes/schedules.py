"""Tasks that start by themselves."""

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
from witx.domain.model import PermissionMode, Repeat
from witx.services import schedules as schedule_service
from witx.services.identity import Actor
from witx.store import schedules as schedule_store

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/schedules")
    async def list_schedules(who: CurrentActor) -> list[api.ScheduleView]:
        async with d.db.tenant(who.tenant_id) as conn:
            return [views.schedule(s) for s in await schedule_store.list_all(conn)]

    @router.post("/api/v1/schedules", status_code=201)
    async def create_schedule(
        body: api.ScheduleCreate, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.ScheduleView:
        """A task that starts by itself: checked now as if it started now."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.ScheduleView:
                model = body.model or d.settings.default_model
                effort = body.reasoning_effort or d.settings.reasoning_effort
                await d.check_model(conn, who.tenant_id, model, effort)
                created = await schedule_service.create(
                    conn,
                    who,
                    goal=body.goal,
                    constraints=body.constraints,
                    device_ids=body.device_ids,
                    permission_mode=PermissionMode(body.permission_mode),
                    model=model,
                    repeat=Repeat(body.repeat),
                    time_of_day=body.time_of_day.replace(second=0, microsecond=0),
                    timezone=body.timezone,
                    weekdays=body.weekdays,
                    month_day=body.month_day,
                    run_on=body.run_on,
                    now=datetime.now(UTC),
                    reasoning_effort=effort,
                )
                return views.schedule(created)

            return await d.once(conn, who, key, request, body, api.ScheduleView, write)

    @router.patch("/api/v1/schedules/{schedule_id}")
    async def update_schedule(
        schedule_id: UUID, body: api.ScheduleUpdate, who: CurrentActor
    ) -> api.ScheduleView:
        """Pause or resume; resuming counts from now."""
        async with d.db.tenant(who.tenant_id) as conn:
            updated = await schedule_service.set_enabled(
                conn, schedule_id, body.enabled, datetime.now(UTC)
            )
            return views.schedule(updated)

    @router.put("/api/v1/schedules/{schedule_id}")
    async def edit_schedule(
        schedule_id: UUID, body: api.ScheduleCreate, who: CurrentActor
    ) -> api.ScheduleView:
        """What it does and when, checked as when it was made; on or off as it was."""
        async with d.db.tenant(who.tenant_id) as conn:
            model = body.model or d.settings.default_model
            effort = body.reasoning_effort or d.settings.reasoning_effort
            await d.check_model(conn, who.tenant_id, model, effort)
            edited = await schedule_service.edit(
                conn,
                schedule_id,
                goal=body.goal,
                constraints=body.constraints,
                device_ids=body.device_ids,
                permission_mode=PermissionMode(body.permission_mode),
                model=model,
                repeat=Repeat(body.repeat),
                time_of_day=body.time_of_day.replace(second=0, microsecond=0),
                timezone=body.timezone,
                weekdays=body.weekdays,
                month_day=body.month_day,
                run_on=body.run_on,
                reasoning_effort=effort,
                now=datetime.now(UTC),
            )
            return views.schedule(edited)

    @router.delete("/api/v1/schedules/{schedule_id}", status_code=204)
    async def delete_schedule(schedule_id: UUID, who: CurrentActor) -> None:
        """The tasks it started stay. Deleting again is not an error."""
        async with d.db.tenant(who.tenant_id) as conn:
            await schedule_store.delete(conn, schedule_id)

    return router
