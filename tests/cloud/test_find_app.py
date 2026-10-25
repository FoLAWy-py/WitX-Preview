"""Opening an app: the agent opens one by the names it goes by, the best match among
the installed apps, or searches them first, in every mode; opening a match
below the best is the user's choice in ask mode, and the agent's in auto and full;
finding nothing sends it to the phone's own search, and then to the user."""

import json
from pathlib import Path
from typing import Any

import pytest
from witx.domain import rules
from witx.domain.model import ActionStatus, Effect, PermissionMode, RunStatus, TaskStatus
from witx.store import identity
from witx.store.db import Database

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision

CALCULATOR = [{"app_id": "com.huawei.calculator", "name": None}]
# The calculator is the second match: the agent chose it over the best one.
SECOND = [{"app_id": "com.huawei.calendar", "name": None}, *CALCULATOR]


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


def find(request: Any, device: Any) -> list[dict[str, Any]]:
    return [
        call(
            "find_app",
            device_id=device,
            observation_id=last_screenshot(request),
            query="计算器 calculator",
            target="the installed apps",
            expected_effect="a list of matching apps",
            effects=[],
        )
    ]


def launch(request: Any, device: Any) -> list[dict[str, Any]]:
    return [
        call(
            "launch_app",
            device_id=device,
            observation_id=last_screenshot(request),
            app_id="com.huawei.calculator",
            target="Calculator",
            expected_effect="the calculator opens",
            effects=[],
        )
    ]


def launch_by_name(request: Any, device: Any) -> list[dict[str, Any]]:
    return [
        call(
            "launch_app",
            device_id=device,
            observation_id=last_screenshot(request),
            query="计算器 calculator",
            app_id=None,
            target="Calculator",
            expected_effect="the calculator opens",
            effects=[],
        )
    ]


async def opened_by_name(rig: Rig, then: Any = None, **task_kw: Any) -> Any:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: launch_by_name(r, device),
        *([then] if then else []),
    ]
    task = await rig.create_task("Open the calculator", **task_kw)
    await rig.settle()
    return task


async def searched(rig: Rig, then: Any = None, **task_kw: Any) -> Any:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        [call("observe", device_id=device)],
        lambda r: find(r, device),
        then or (lambda r: launch(r, device)),
    ]
    task = await rig.create_task("Open the calculator", **task_kw)
    await rig.settle()
    return task


async def test_when_no_installed_app_matches_the_agent_is_told_to_use_the_phone_s_search(
    rig: Rig,
) -> None:
    asks = call("ask_user", question="This phone has no calculator. Try another phone?")
    task = await searched(rig, then=[asks])
    assert (await rig.task(task.id)).status == TaskStatus.ACTIVE
    run = await rig.root_run(task.id)
    assert run.status == RunStatus.WAITING  # on the user's answer, not failed
    assert len(rig.model.requests) == 3  # the model was asked what to do next
    told = json.loads(
        next(
            i["output"]
            for i in rig.model.requests[2].input
            if i.get("type") == "function_call_output" and '"apps": []' in i["output"]
        )
    )
    assert "swipe down from the middle" in told["note"] and "ask_user" in told["note"]
    (search,) = await rig.actions(task.id)
    assert search.effect == Effect.NO_EFFECT and search.found_apps == []


async def test_the_agent_sees_the_matches_and_opens_the_one_it_chose_without_asking(
    rig: Rig,
) -> None:
    rig.gateway.found_apps = SECOND
    task = await searched(rig)  # auto, the default
    outputs = await rig.outputs(task.id)
    assert outputs[1]["apps"] == SECOND
    search, opening = await rig.actions(task.id)
    assert search.status == ActionStatus.DONE
    assert opening.status == ActionStatus.DONE
    assert opening.policy is not None and opening.policy.found_rank == 1


async def test_in_ask_mode_the_search_runs_and_its_best_match_opens_without_asking(
    rig: Rig,
) -> None:
    rig.gateway.found_apps = CALCULATOR
    task = await searched(rig, permission_mode=PermissionMode.ASK)
    search, opening = await rig.actions(task.id)
    assert search.status == ActionStatus.DONE and opening.status == ActionStatus.DONE


async def test_in_ask_mode_opening_a_lower_match_waits_for_the_user(rig: Rig) -> None:
    rig.gateway.found_apps = SECOND
    task = await searched(rig, permission_mode=PermissionMode.ASK)
    search, opening = await rig.actions(task.id)
    assert search.status == ActionStatus.DONE
    assert opening.status == ActionStatus.AWAITING_APPROVAL


async def test_with_full_access_a_lower_match_opens_without_asking(
    rig: Rig,
    admin_db: Database,
) -> None:
    async with admin_db.system() as conn:
        await identity.set_allow_full_access(conn, rig.tenant.id, True)
    rig.gateway.found_apps = SECOND
    task = await searched(rig, permission_mode=PermissionMode.FULL)
    _, opening = await rig.actions(task.id)
    assert opening.status == ActionStatus.DONE


async def test_opening_an_app_by_name_opens_the_best_match_and_shows_the_others(
    rig: Rig,
) -> None:
    rig.gateway.found_apps = SECOND
    task = await opened_by_name(rig, permission_mode=PermissionMode.ASK)
    (opening,) = await rig.actions(task.id)
    assert opening.status == ActionStatus.DONE  # the best match: no approval, even in ask
    assert opening.policy is not None and opening.policy.found_rank == 0
    assert opening.found_apps == SECOND
    told = (await rig.outputs(task.id))[1]
    assert told["apps"] == SECOND and "was opened" in told["note"]


async def test_in_ask_mode_opening_another_match_of_a_launch_by_name_waits_for_the_user(
    rig: Rig,
) -> None:
    rig.gateway.found_apps = SECOND
    device = rig.tenant.device_ids[0]
    task = await opened_by_name(
        rig, then=lambda r: launch(r, device), permission_mode=PermissionMode.ASK
    )
    _, opening = await rig.actions(task.id)
    assert opening.policy is not None and opening.policy.found_rank == 1
    assert opening.status == ActionStatus.AWAITING_APPROVAL


async def test_when_no_installed_app_matches_a_launch_by_name_the_agent_is_told_to_search(
    rig: Rig,
) -> None:
    asks = call("ask_user", question="This phone has no calculator. Try another phone?")
    task = await opened_by_name(rig, then=[asks])
    assert (await rig.root_run(task.id)).status == RunStatus.WAITING  # not failed
    (opening,) = await rig.actions(task.id)
    assert opening.effect == Effect.NOT_ATTEMPTED and opening.reason == "app_not_found"
    told = (await rig.outputs(task.id))[1]
    assert "swipe down from the middle" in told["note"] and "ask_user" in told["note"]


@pytest.mark.parametrize(
    ("mode", "kind", "rank", "decision"),
    [
        (PermissionMode.ASK, "find_app", None, "allow"),
        (PermissionMode.AUTO, "find_app", None, "allow"),
        (PermissionMode.FULL, "find_app", None, "allow"),
        (PermissionMode.ASK, "launch_app", 0, "allow"),  # the best match
        (PermissionMode.ASK, "launch_app", 1, "ask"),
        (PermissionMode.AUTO, "launch_app", 0, "allow"),
        (PermissionMode.AUTO, "launch_app", 1, "allow"),  # not the best match
        (PermissionMode.FULL, "launch_app", 0, "allow"),
        (PermissionMode.FULL, "launch_app", 1, "allow"),
        (PermissionMode.ASK, "launch_app", None, "allow"),  # an app the agent knew
    ],
)
def test_searching_runs_in_every_mode_and_only_ask_mode_holds_a_lower_match(
    mode: PermissionMode, kind: str, rank: int | None, decision: str
) -> None:
    policy = rules.decide_policy(
        mode=mode,
        kind=kind,
        declared_effects=[],
        risk=None,
        app_sensitive=False,
        text_flags=[],
        found_rank=rank,
    )
    assert policy.decision == decision
    assert rules.reapply_policy(policy, mode, kind).found_rank == rank  # kept on mode switch


def test_a_found_app_ranks_by_the_newest_search_that_found_it() -> None:
    newest = [{"app_id": "b"}, {"app_id": "a"}]
    older = [{"app_id": "a"}]
    assert rules.found_rank("a", [newest, older]) == 1
    assert rules.found_rank("c", [newest, older]) is None
