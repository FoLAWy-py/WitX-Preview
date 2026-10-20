"""Questions about a task in a side tab."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from witx_contracts import api

from witx.api import views
from witx.api.deps import (
    Deps,
    IdempotencyKey,
)
from witx.services import side_chat as side_chat_service
from witx.services.identity import Actor
from witx.store import side_chats as side_chat_store

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/tasks/{task_id}/side-chat")
    async def side_chat_messages(task_id: UUID, who: CurrentActor) -> list[api.SideMessageView]:
        """Questions asked about the task in side chats, and their answers."""
        async with d.db.tenant(who.tenant_id) as conn:
            return [views.side_message(m) for m in await side_chat_store.for_task(conn, task_id)]

    @router.post("/api/v1/tasks/{task_id}/side-chat", status_code=201)
    async def ask_side_chat(
        task_id: UUID,
        body: api.SideChatAsk,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.SideMessageView:
        """Asks about the task; the worker answers. It never acts and never enters the
        task's conversation."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.SideMessageView:
                asked = await side_chat_service.ask(
                    conn,
                    task_id=task_id,
                    thread_id=body.thread_id,
                    user_id=who.user_id,
                    question=body.text,
                )
                return views.side_message(asked)

            return await d.once(conn, who, key, request, body, api.SideMessageView, write)

    @router.delete("/api/v1/tasks/{task_id}/side-chat/{thread_id}", status_code=204)
    async def close_side_chat(task_id: UUID, thread_id: UUID, who: CurrentActor) -> None:
        """Closing a side chat's tab forgets it. Closing again is not an error."""
        async with d.db.tenant(who.tenant_id) as conn:
            await side_chat_store.delete_thread(conn, task_id, thread_id)

    return router
