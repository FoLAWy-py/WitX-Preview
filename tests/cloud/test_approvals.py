"""Approval semantics : an approval is a person's, bound to one input,
used at most once, and only while it is valid."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from witx.domain.model import ActionStatus
from witx.domain.rules import Rejected
from witx.services import execution as exec_service
from witx.services import lifecycle
from witx.store import execution, runs
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, now, provision
from tests.cloud.test_flow import tap


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def held(rig: Rig) -> UUID:
    """A task whose message-sending tap waits for approval."""
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: tap(r, device, effects=["delete_data"]),
    ]
    task = await rig.create_task("Send the note")
    await rig.settle()
    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.AWAITING_APPROVAL
    return action.id


async def decide(rig: Rig, action_id: UUID, decision: str = "approve", **kw: object) -> None:
    async with rig.db.tenant(rig.tenant.id) as conn:
        await exec_service.decide(
            conn,
            action_id=action_id,
            decision=decision,
            user_id=rig.tenant.user_id,
            now=kw.pop("at", now()),  # type: ignore[arg-type]
        )


async def test_an_approval_that_came_too_late_is_refused_and_the_input_is_cancelled(
    rig: Rig,
) -> None:
    action_id = await held(rig)
    later = now() + rig.limits.approval_ttl + timedelta(seconds=1)
    with pytest.raises(Rejected, match="approval_expired"):
        await decide(rig, action_id, at=later)
    async with rig.db.tenant(rig.tenant.id) as conn:
        action = await execution.get_action(conn, action_id)
        assert action is not None
        run = await runs.get(conn, action.run_id)
        assert run is not None
        await exec_service.expire_approvals(conn, run, later)
        cancelled = await execution.get_action(conn, action_id)
    assert cancelled is not None and cancelled.status == ActionStatus.CANCELLED
    assert cancelled.reason == "approval_expired"
    await rig.settle()
    assert rig.gateway.actions == []  # never reached the device


async def test_an_input_is_approved_once_and_sent_once(rig: Rig) -> None:
    action_id = await held(rig)
    await decide(rig, action_id)
    with pytest.raises(Rejected, match="not_awaiting_approval"):
        await decide(rig, action_id)  # a repeated approval
    with pytest.raises(Rejected, match="not_awaiting_approval"):
        await decide(rig, action_id, "deny")  # a change of mind after it was approved
    await rig.settle()
    await rig.settle()
    assert [a.action_id for a in rig.gateway.actions] == [action_id]
    async with rig.db.tenant(rig.tenant.id) as conn:
        approval = await execution.get_approval(conn, action_id)
    assert approval is not None and approval.used_at is not None  # spent when sent


async def test_two_people_approving_at_the_same_moment_send_it_once(rig: Rig) -> None:
    action_id = await held(rig)
    outcomes = await asyncio.gather(
        decide(rig, action_id), decide(rig, action_id), return_exceptions=True
    )
    refused = [o for o in outcomes if isinstance(o, Rejected)]
    assert len(refused) == 1 and refused[0].code == "not_awaiting_approval"
    await rig.settle()
    assert [a.action_id for a in rig.gateway.actions] == [action_id]


async def test_another_tenant_cannot_approve_this_tenants_input(
    rig: Rig, db: Database, admin_db: Database
) -> None:
    action_id = await held(rig)
    other = await provision(admin_db, "globex", devices=1)
    async with db.tenant(other.id) as conn:
        with pytest.raises(Rejected, match="not_found"):
            await exec_service.decide(
                conn, action_id=action_id, decision="approve", user_id=other.user_id, now=now()
            )
    await rig.settle()
    assert rig.gateway.actions == []


async def test_stopping_the_task_after_approval_but_before_sending_cancels_the_input(
    rig: Rig,
) -> None:
    action_id = await held(rig)
    await decide(rig, action_id)  # approved, not yet handed to the Gateway
    async with rig.db.tenant(rig.tenant.id) as conn:
        action = await execution.get_action(conn, action_id)
        assert action is not None and action.status == ActionStatus.QUEUED
        await lifecycle.stop(conn, action.task_id)
    await rig.settle()
    assert rig.gateway.actions == []
    async with rig.db.tenant(rig.tenant.id) as conn:
        stopped = await execution.get_action(conn, action_id)
    assert stopped is not None and stopped.status in (ActionStatus.CANCELLED, ActionStatus.REJECTED)
