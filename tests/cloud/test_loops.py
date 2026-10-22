"""Going in circles: a cycle of inputs repeated since the user's last message
warns the agent, then stops the task for the user."""

from pathlib import Path
from typing import Any

import pytest
from witx.domain import rules
from witx.domain.model import RunStatus, WaitReason
from witx.store.db import Database

from tests.cloud.rig import Rig
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_flow import complete, tap


def t(x: float, y: float) -> dict[str, Any]:
    return {"kind": "tap", "x": x, "y": y}


def typed(text: str) -> dict[str, Any]:
    return {"kind": "type_text", "text": text}


def test_a_cycle_repeated_back_to_back_is_counted_even_when_taps_wander_along_a_row() -> None:
    # What a run did in #74: add phone, clear the name, type the name again, 20 times.
    cycle = [[t(x, 1128), t(670, 797), typed("测试102")] for x in (248, 234, 215, 384, 99)]
    assert rules.repeats([i for c in cycle for i in c]) == 5


def test_typing_the_same_text_another_way_is_not_going_in_circles() -> None:
    cycle = [t(300, 900), typed("好看"), t(300, 900), typed("好看") | {"method": "keys"}]
    assert rules.repeats(cycle) == 1


def test_one_input_repeated_on_its_own_is_not_going_in_circles() -> None:
    assert rules.repeats([t(600, 400)] * 8) == 1  # e.g. tapping "+" eight times


def test_scrolling_between_taps_breaks_a_cycle() -> None:
    swipe = {"kind": "swipe", "x1": 300, "y1": 1500, "x2": 300, "y2": 500, "duration_ms": 300}
    assert rules.repeats([t(300, 900), swipe] * 6) == 1


def test_working_through_different_items_is_not_a_cycle() -> None:
    inputs = [i for y in (300, 450, 600, 750) for i in (t(200, y), t(380, 1500))]
    assert rules.repeats(inputs) == 1


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    await rig.connect([device_info(0)])
    return rig


async def test_an_agent_going_in_circles_is_warned_then_stopped_for_the_user(rig: Rig) -> None:
    device = rig.tenant.device_ids[0]
    back_and_forth = [
        lambda r, y=y: tap(r, device, y=y) for _ in range(rules.LOOP_ASK) for y in (900, 300)
    ]
    rig.model.script = [*back_and_forth, complete]
    task = await rig.create_task()
    await rig.settle(rounds=60)

    outputs = [o for o in await rig.outputs(task.id) if o["name"] == "tap"]
    warned = [i for i, o in enumerate(outputs) if "warning" in o]
    assert warned and warned[0] == 2 * rules.LOOP_WARN - 1  # the input completing the 4th
    assert outputs[-1]["status"] == "rejected" and outputs[-1]["code"] == "repeating"
    assert len(await rig.actions(task.id)) == 2 * rules.LOOP_ASK - 1  # the 16th never ran
    run = await rig.root_run(task.id)
    assert (run.status, run.wait_reason, run.wait_detail) == (
        RunStatus.WAITING,
        WaitReason.USER_INPUT,
        "repeating",
    )
    assert "run.repeating" in await rig.event_types(task.id)


# ---------------------------------------------------------------- an unchanged screen


def test_tapping_the_same_place_again_on_an_unchanged_screen_is_refused() -> None:
    # #313's baseline: a Dark theme switch showed off 1.7 s after the tap; a second tap
    # turned it back.
    with pytest.raises(rules.Rejected) as refused:
        rules.check_changed(t(669, 951), t(664, 950), before="same", after="same")
    assert refused.value.code == "unchanged_screen"


def test_tapping_again_after_the_screen_changed_goes_ahead() -> None:
    rules.check_changed(t(669, 951), t(664, 950), before="off", after="on")


def test_tapping_somewhere_else_on_an_unchanged_screen_goes_ahead() -> None:
    rules.check_changed(t(200, 951), t(664, 950), before="same", after="same")


async def test_after_a_refused_tap_on_an_unchanged_screen_observing_lets_the_tap_go_ahead(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    rig.gateway.same_picture = True
    rig.model.script = [
        lambda r: tap(r, device),
        lambda r: tap(r, device),
        [call("observe", device_id=device)],
        lambda r: tap(r, device),
        complete,
    ]
    task = await rig.create_task()
    await rig.settle()

    outputs = [o for o in await rig.outputs(task.id) if o["name"] == "tap"]
    assert outputs[1]["status"] == "rejected" and outputs[1]["code"] == "unchanged_screen"
    assert len(await rig.actions(task.id)) == 2
