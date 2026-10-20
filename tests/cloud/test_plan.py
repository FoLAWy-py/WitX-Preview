"""Plan mode: the user wants a plan before any action. The agent looks and presents
a plan; its inputs are refused until the user approves it."""

import pytest
from witx.domain import rules
from witx.domain.model import RiskVerdict, RunStatus, WaitReason
from witx.domain.rules import Rejected
from witx.services import lifecycle

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision, say
from tests.cloud.test_flow import complete, tap


@pytest.fixture
async def rig(db, admin_db, tmp_path) -> Rig:  # type: ignore[no-untyped-def]
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


def present(request):  # type: ignore[no-untyped-def]
    return [call("present_plan", plan="1. Open Settings\n2. Open Wi-Fi")]


async def say_to_agent(rig: Rig, task_id, text: str) -> None:  # type: ignore[no-untyped-def]
    async with rig.db.tenant(rig.tenant.id) as conn:
        await lifecycle.add_input(conn, task_id=task_id, user_id=rig.tenant.user_id, text=text)


def test_plan_commands_are_read_from_the_users_message() -> None:
    assert rules.plan_command("/plan Open Wi-Fi") == rules.PlanCommand("start", "Open Wi-Fi")
    assert rules.plan_command("/plan approve") == rules.PlanCommand("approve")
    assert rules.plan_command("/Plan OFF") == rules.PlanCommand("off")
    assert rules.plan_command("Open Wi-Fi, then /plan approve") is None
    with pytest.raises(Rejected):
        rules.plan_command("/plan")


def looking(**kw: object) -> None:
    values: dict[str, object] = {
        "kind": "tap",
        "declared_effects": [],
        "risk": RiskVerdict(sensitive=False, effects=[], reason="opens a page"),
        "app_sensitive": False,
        "text_flags": [],
    }
    values.update(kw)
    rules.check_only_looking(**values)  # type: ignore[arg-type]


def test_in_plan_mode_an_input_that_changes_nothing_is_not_refused() -> None:
    looking()
    looking(kind="launch_app", risk=None)  # not an assessed kind: no verdict to wait for
    looking(kind="find_app", risk=None)
    looking(kind="type_text")  # a search word


@pytest.mark.parametrize(
    "overrides",
    [
        {"declared_effects": [rules.CHANGE_EFFECT]},
        {"declared_effects": ["send_message"]},
        {"risk": RiskVerdict(sensitive=True, effects=[], reason="could send")},
        {"risk": RiskVerdict(sensitive=False, effects=[rules.CHANGE_EFFECT], reason="saves")},
        {"risk": None},  # an input that was not assessed is never taken for harmless
        {"app_sensitive": True},
        {"kind": "type_text", "text_flags": ["phone_number"]},
    ],
)
def test_in_plan_mode_an_input_that_could_change_something_is_refused(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(Rejected) as refused:
        looking(**overrides)
    assert refused.value.code == "plan_mode"


async def test_a_task_started_with_plan_looks_around_but_changes_nothing_until_approved(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        lambda r: tap(r, device),  # looking around: goes through
        lambda r: tap(r, device, effects=[rules.CHANGE_EFFECT]),  # a change: refused
        present,
        lambda r: tap(r, device, effects=[rules.CHANGE_EFFECT]),
        complete,
    ]
    task = await rig.create_task("/plan Open Wi-Fi settings")
    assert task.plan_mode and task.request == "Open Wi-Fi settings"
    await rig.settle()

    looked, refused, presented = (await rig.outputs(task.id))[:3]
    assert looked["status"] == "done"
    assert (refused["status"], refused["code"]) == ("rejected", "plan_mode")
    assert presented["status"] == "presented"
    (only,) = await rig.actions(task.id)  # the change was never stored
    assert only.effects == []
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason, run.wait_detail) == (
        RunStatus.WAITING,
        WaitReason.USER_INPUT,
        "plan",
    )
    assert "plan.proposed" in await rig.event_types(task.id)

    await say_to_agent(rig, task.id, "/plan approve")
    await rig.settle()
    assert not (await rig.task(task.id)).plan_mode
    assert len(await rig.actions(task.id)) == 2  # now the change was admitted
    assert "The user approved your plan" in str(rig.model.requests[-1].input)


async def test_a_plan_cannot_be_approved_before_the_agent_presents_one(rig: Rig) -> None:
    task = await rig.create_task("/plan Open Wi-Fi settings")
    with pytest.raises(Rejected) as refused:
        await say_to_agent(rig, task.id, "/plan approve")
    assert refused.value.code == "no_plan"
    assert (await rig.task(task.id)).plan_mode


async def test_a_plan_cannot_be_approved_when_the_task_is_not_in_plan_mode(rig: Rig) -> None:
    task = await rig.create_task("Open Wi-Fi settings")
    with pytest.raises(Rejected) as refused:
        await say_to_agent(rig, task.id, "/plan approve")
    assert refused.value.code == "not_planning"


async def test_the_user_can_leave_plan_mode_without_a_plan(rig: Rig) -> None:
    task = await rig.create_task("/plan Open Wi-Fi settings")
    await say_to_agent(rig, task.id, "/plan off")
    assert not (await rig.task(task.id)).plan_mode


async def test_a_message_can_turn_plan_mode_on_for_a_task_that_is_under_way(rig: Rig) -> None:
    task = await rig.create_task("Open Wi-Fi settings")
    await say_to_agent(rig, task.id, "/plan Then turn Wi-Fi off")
    assert (await rig.task(task.id)).plan_mode
    assert "task.plan_mode" in await rig.event_types(task.id)


async def test_presenting_a_plan_outside_plan_mode_is_refused(rig: Rig) -> None:
    rig.model.script = [present, complete]
    task = await rig.create_task("Open Wi-Fi settings")
    await rig.settle()
    (first, *_) = await rig.outputs(task.id)
    assert (first["status"], first["code"]) == ("rejected", "not_planning")


async def test_a_sub_agent_cannot_present_the_plan(rig: Rig) -> None:
    device = str(rig.tenant.device_ids[0])
    rig.model.script = [
        [call("delegate", subtasks=[{"goal": "Look at Wi-Fi", "device_ids": [device]}])],
        present,  # the child's first turn
        [say("Nothing to report.")],  # its second: it stops for the user
    ]
    task = await rig.create_task("/plan Open Wi-Fi settings")
    await rig.settle()
    child = [o for o in await rig.outputs(task.id) if o.get("code") == "not_planning"]
    assert len(child) == 1
