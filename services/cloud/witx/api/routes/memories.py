"""A person's long-term memory (§8.2, D59): their own entries and what agents kept."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from witx_contracts import api

from witx.api import views
from witx.api.deps import Deps, IdempotencyKey
from witx.services import memories as memory_service
from witx.services.identity import Actor
from witx.store import identity, memories


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.get("/api/v1/memories")
    async def list_memories(who: CurrentActor) -> list[api.MemoryView]:
        """The signed-in person's kept entries; nobody else's."""
        async with d.db.tenant(who.tenant_id) as conn:
            return [views.memory_view(m) for m in await memories.of_user(conn, who.user_id)]

    @router.post("/api/v1/memories", status_code=201)
    async def add_memory(
        body: api.MemoryCreate, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.MemoryView:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.MemoryView:
                entry = await memory_service.add(
                    conn, tenant_id=who.tenant_id, user_id=who.user_id, text=body.text
                )
                return views.memory_view(entry)

            return await d.once(conn, who, key, request, body, api.MemoryView, write)

    @router.put("/api/v1/memories/settings")
    async def memory_settings(body: api.MemorySettings, who: CurrentActor) -> api.MemorySettings:
        """Whether agents keep what they learn for the signed-in person on their own."""
        async with d.db.system() as conn:
            await identity.set_auto_memory(conn, who.tenant_id, who.user_id, body.auto_memory)
            return api.MemorySettings(
                auto_memory=await identity.auto_memory(conn, who.tenant_id, who.user_id)
            )

    @router.delete("/api/v1/memories/{memory_id}", status_code=204)
    async def delete_memory(memory_id: UUID, who: CurrentActor) -> None:
        async with d.db.tenant(who.tenant_id) as conn:
            await memory_service.delete(conn, memory_id=memory_id, user_id=who.user_id)

    return router
