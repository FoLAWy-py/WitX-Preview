"""What a workspace's pages hear of as it changes."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from witx_contracts import api

from witx.api.deps import Deps
from witx.services.identity import Actor

KEEP_ALIVE_S = 15


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/workspace/events")
    async def workspace_events(who: CurrentActor) -> StreamingResponse:
        """Server-sent events, one each time something the workspace's pages show
        changed: its ``type`` is what (``task``, ``groups``, ``devices``, ``schedules``,
        or ``all``: load everything), its data the task and the task's event type. They
        carry no content and are not kept; ``seq`` counts within one connection. The
        stream opens with a comment once it listens: a page loads what it shows after
        that, so nothing committed in between is missed."""

        async def stream() -> AsyncIterator[bytes]:
            page = await d.workspace.subscribe(who.tenant_id)
            try:
                yield b": listening\n\n"
                seq = 0
                while True:
                    try:
                        notice = await asyncio.wait_for(page.notices.get(), KEEP_ALIVE_S)
                    except TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    seq += 1
                    event = api.EventView(
                        seq=seq,
                        type=notice.kind,
                        data={"task_id": notice.task_id, "type": notice.type},
                        created_at=datetime.now(UTC),
                    )
                    frame = f"id: {seq}\nevent: {notice.kind}\ndata: {event.model_dump_json()}"
                    yield (frame + "\n\n").encode()
            finally:
                d.workspace.unsubscribe(page)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    return router
