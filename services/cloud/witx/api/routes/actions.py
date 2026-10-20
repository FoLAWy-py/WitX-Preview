"""Decisions people make about held inputs: approvals, purchases, grants, unknown effects."""

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
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.services import purchases as purchase_service
from witx.services.identity import Actor
from witx.store import execution, tasks

log = logging.getLogger(__name__)


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.post("/api/v1/actions/{action_id}/approval")
    async def decide(
        action_id: UUID,
        body: api.ApprovalDecision,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.ActionView:
        now = datetime.now(UTC)
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.ActionView:
                action = await exec_service.decide(
                    conn,
                    action_id=action_id,
                    decision=body.decision,
                    user_id=who.user_id,
                    now=now,
                    similar=body.similar,
                )
                approval = await execution.get_approval(conn, action_id)
                return views.action_view(action, approval, now)

            return await d.once(conn, who, key, request, body, api.ActionView, write)

    @router.post("/api/v1/purchases/{purchase_id}/choice")
    async def choose_purchase(
        purchase_id: UUID,
        body: api.PurchaseChoice,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.PurchaseView:
        """A person chooses where to buy (or nothing); the agent reads it as a message."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.PurchaseView:
                intent = await purchase_service.choose(
                    conn,
                    intent_id=purchase_id,
                    option=body.option,
                    user_id=who.user_id,
                    now=datetime.now(UTC),
                )
                return views.purchase_view(intent, newest=True)

            return await d.once(conn, who, key, request, body, api.PurchaseView, write)

    @router.post("/api/v1/grants/{grant_id}/revoke")
    async def revoke_grant(
        grant_id: UUID, who: CurrentActor, request: Request, key: IdempotencyKey = None
    ) -> api.GrantView:
        """Ends a standing approval at once; inputs it approved that are not sent yet
        wait for a person again."""
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.GrantView:
                grant = await exec_service.revoke_grant(
                    conn, grant_id=grant_id, user_id=who.user_id
                )
                task = await tasks.get(conn, grant.task_id)
                assert task is not None
                return views.grant_view(grant, task, datetime.now(UTC))

            return await d.once(conn, who, key, request, None, api.GrantView, write)

    @router.post("/api/v1/actions/{action_id}/reconcile")
    async def reconcile(
        action_id: UUID,
        body: api.Reconcile,
        who: CurrentActor,
        request: Request,
        key: IdempotencyKey = None,
    ) -> api.ActionView:
        async with d.db.tenant(who.tenant_id) as conn:

            async def write() -> api.ActionView:
                action = await exec_service.reconcile(
                    conn,
                    action_id=action_id,
                    effect=body.effect,
                    observation_id=body.observation_id,
                    user_id=who.user_id,
                )
                await lifecycle.add_input(
                    conn,
                    task_id=action.task_id,
                    user_id=who.user_id,
                    text=f"I checked action {action.id}: its effect was "
                    f"{body.effect.replace('_', ' ')} (see screenshot {body.observation_id}).",
                    run_id=action.run_id,
                    system=True,
                )
                return views.action_view(
                    action, await execution.get_approval(conn, action_id), datetime.now(UTC)
                )

            return await d.once(conn, who, key, request, body, api.ActionView, write)

    return router
