"""A task with several phones and one goal asks which phone does it, unless it has full
access, which asks nothing."""

from pathlib import Path

import pytest
from witx.domain.model import PermissionMode
from witx.harness import context
from witx.models import ModelRequest
from witx.store import identity
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete
from tests.cloud.test_parallel import agent


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=2)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0), device_info(1)])
    async with admin_db.system() as conn:
        await identity.set_allow_full_access(conn, tenant.id, True)
    return rig


def told(request: ModelRequest) -> bool:
    """Whether the request's first message carries the rule."""
    return context.WHICH_PHONE in str(request.input[0]["content"][0]["text"])


async def first_request(rig: Rig, **task: object) -> ModelRequest:
    rig.model.script = [complete]
    await rig.create_task(**task)
    await rig.settle()
    return rig.model.requests[0]


@pytest.mark.parametrize("mode", [PermissionMode.ASK, PermissionMode.AUTO])
async def test_below_full_access_the_agent_is_told_to_ask_which_of_several_phones(
    rig: Rig, mode: PermissionMode
) -> None:
    assert told(await first_request(rig, permission_mode=mode))


async def test_with_full_access_the_agent_is_not_told_to_ask(rig: Rig) -> None:
    assert not told(await first_request(rig, permission_mode=PermissionMode.FULL))


async def test_with_one_phone_the_agent_is_not_told_to_ask(rig: Rig) -> None:
    first = rig.tenant.device_ids[0]
    assert not told(await first_request(rig, device_ids=[first]))


async def test_a_sub_agent_is_not_told_to_ask_which_phone(rig: Rig) -> None:
    a, b = (str(d) for d in rig.tenant.device_ids)
    delegate = call(
        "delegate",
        subtasks=[
            {"goal": "Phone A: check it", "device_id": a},
            {"goal": "Phone B: check it", "device_id": b},
        ],
    )
    rig.model.script = [agent(rig, delegate)] * 12
    await rig.create_task(permission_mode=PermissionMode.AUTO)
    await rig.worker.run_once()
    await rig.gateway_round()
    await rig.worker.run_once()
    await rig.settle()
    asked = [r for r in rig.model.requests if told(r)]
    assert len(asked) >= 1 and all("Phone" not in str(r.input[0]["content"]) for r in asked)
