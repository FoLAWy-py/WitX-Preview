"""Held swipes, long presses and double taps: offered to the agent, bounded, judged
like a tap and delivered with their timing."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from witx.domain import rules
from witx.domain.model import ActionStatus, PermissionMode, RiskVerdict
from witx.domain.rules import Rejected
from witx.harness import tools
from witx.harness.risk import describe_input
from witx.store.db import Database
from witx_contracts.common import ActionInput

from tests.cloud.rig import Rig, last_screenshot
from tests.cloud.support import call, device_info, provision
from tests.cloud.test_rules import DEVICE, admit

inputs: TypeAdapter[ActionInput] = TypeAdapter(ActionInput)
SWIPE = {"kind": "swipe", "x1": 100, "y1": 900, "x2": 100, "y2": 800, "duration_ms": 200}


def test_a_swipe_lifts_at_once_unless_it_is_told_to_hold() -> None:
    assert inputs.validate_python(SWIPE).model_dump()["hold_ms"] == 0
    assert inputs.validate_python(SWIPE | {"hold_ms": 15000}).model_dump()["hold_ms"] == 15000


@pytest.mark.parametrize(
    "given",
    [
        SWIPE | {"hold_ms": -1},
        SWIPE | {"hold_ms": 15001},
        {"kind": "long_press", "x": 10, "y": 10, "duration_ms": 499},
        {"kind": "long_press", "x": 10, "y": 10, "duration_ms": 15001},
        {"kind": "long_press", "x": 10, "y": 10},
        {"kind": "double_tap", "x": -1, "y": 10},
        {"kind": "double_tap", "x": 10, "y": 10, "duration_ms": 100},
        {"kind": "pinch", "x": 10, "y": 10, "scale": 0.1},
        {"kind": "pinch", "x": 10, "y": 10, "scale": 6},
        {"kind": "pinch", "x": 10, "y": 10, "scale": 1},
        {"kind": "pinch", "x": 10, "y": 10},
    ],
)
def test_gestures_out_of_their_bounds_are_not_valid_inputs(given: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        inputs.validate_python(given)


def offered(name: str) -> dict[str, Any]:
    (tool,) = [t for t in tools.TOOLS if t["name"] == name]
    return tool


def test_the_agent_is_offered_long_press_double_tap_and_a_held_swipe() -> None:
    assert {"x", "y", "duration_ms"} <= set(offered("long_press")["parameters"]["required"])
    assert {"x", "y"} <= set(offered("double_tap")["parameters"]["required"])
    assert "duration_ms" not in offered("double_tap")["parameters"]["properties"]
    assert "hold_ms" in offered("swipe")["parameters"]["required"]
    assert {"long_press", "double_tap", "swipe"} <= set(tools.INPUT_TOOLS)


def input_args(**kw: Any) -> dict[str, Any]:
    return {
        "device_id": str(DEVICE.id),
        "observation_id": str(DEVICE.id),
        "target": "joystick",
        "expected_effect": "the character walks left",
        "effects": [],
        "intent": None,
        "notes": None,
        **kw,
    }


def test_a_held_swipe_and_a_long_press_keep_their_timing() -> None:
    swipe = tools.parse_input(
        "swipe", input_args(x1=200, y1=2000, x2=120, y2=2000, duration_ms=150, hold_ms=4000)
    )
    assert (swipe.payload["duration_ms"], swipe.payload["hold_ms"]) == (150, 4000)
    press = tools.parse_input("long_press", input_args(x=540, y=1200, duration_ms=800))
    assert press.payload == {"kind": "long_press", "x": 540.0, "y": 1200.0, "duration_ms": 800}
    double = tools.parse_input("double_tap", input_args(x=540, y=1200))
    assert double.payload == {"kind": "double_tap", "x": 540.0, "y": 1200.0}


def test_the_agents_swipe_has_no_path_even_when_it_sends_one() -> None:
    # The path is a person's drag; the tool does not offer it, and like any argument
    # the tool does not have, one the model sends anyway is left out, not held against it.
    assert "path" not in offered("swipe")["parameters"]["properties"]
    swipe = tools.parse_input(
        "swipe",
        input_args(
            x1=100,
            y1=900,
            x2=100,
            y2=800,
            duration_ms=200,
            hold_ms=0,
            path=[{"x": 100, "y": 900, "t_ms": 0}, {"x": 100, "y": 800, "t_ms": 200}],
        ),
    )
    assert swipe.payload["path"] is None


def test_every_point_of_a_swipes_path_must_be_on_the_screenshot() -> None:
    swipe = SWIPE | {"path": [{"x": 100, "y": 900, "t_ms": 0}, {"x": 100, "y": 800, "t_ms": 200}]}
    rules.check_points(swipe, 1080, 2400)
    swipe["path"].insert(1, {"x": 1080, "y": 850, "t_ms": 100})  # type: ignore[attr-defined]
    with pytest.raises(Rejected) as refused:
        rules.check_points(swipe, 1080, 2400)
    assert refused.value.code == "out_of_bounds"


@pytest.mark.parametrize(
    ("name", "args", "where"),
    [
        (
            "swipe",
            {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 100, "hold_ms": 20000},
            "hold_ms",
        ),
        ("long_press", {"x": 1, "y": 2, "duration_ms": 100}, "duration_ms"),
        ("long_press", {"x": 1, "y": 2, "duration_ms": 60000}, "duration_ms"),
    ],
)
def test_a_gesture_held_too_short_or_too_long_is_refused(
    name: str, args: dict[str, Any], where: str
) -> None:
    with pytest.raises(Rejected) as refused:
        tools.parse_input(name, input_args(**args))
    assert refused.value.code == "invalid_arguments" and where in (refused.value.detail or "")


@pytest.mark.parametrize("kind", ["long_press", "double_tap"])
def test_a_press_is_judged_like_a_tap(kind: str) -> None:
    assert kind in rules.ASSESSED_KINDS
    for mode in (PermissionMode.AUTO, PermissionMode.FULL):
        unassessed = rules.decide_policy(
            mode=mode, kind=kind, declared_effects=[], risk=None, app_sensitive=False, text_flags=[]
        )
        assert unassessed.decision == "ask"  # never assumed harmless
    flagged = rules.decide_policy(
        mode=PermissionMode.AUTO,
        kind=kind,
        declared_effects=[],
        risk=RiskVerdict(sensitive=True, effects=["delete_data"], reason="Delete in the menu"),
        app_sensitive=False,
        text_flags=[],
    )
    assert flagged.decision == "ask"
    with pytest.raises(Rejected, match="plan_mode"):
        rules.check_only_looking(
            kind=kind, declared_effects=[], risk=None, app_sensitive=False, text_flags=[]
        )


def test_presses_on_the_same_row_count_as_the_same_input_when_going_in_circles() -> None:
    press = {"kind": "long_press", "x": 100, "y": 500, "duration_ms": 800}
    assert rules.same_input(press, press | {"x": 400, "duration_ms": 1500})
    assert not rules.same_input(press, press | {"y": 900})
    double = {"kind": "double_tap", "x": 100, "y": 500}
    assert rules.same_input(double, double | {"x": 300})


def test_a_press_outside_the_screenshot_is_refused() -> None:
    device = replace(DEVICE, capabilities=["long_press", "double_tap"])
    for payload in (
        {"kind": "long_press", "x": 1080.0, "y": 10.0, "duration_ms": 800},
        {"kind": "double_tap", "x": 10.0, "y": 2400.0},
    ):
        with pytest.raises(Rejected, match="out_of_bounds"):
            admit(device=device, payload=payload)


def test_the_risk_check_is_told_how_long_a_press_or_swipe_holds() -> None:
    assert "for 800 ms" in describe_input(
        {"kind": "long_press", "x": 1, "y": 2, "duration_ms": 800}
    )
    assert "double tap" in describe_input({"kind": "double_tap", "x": 1, "y": 2})
    assert "hold there for 3000 ms" in describe_input(SWIPE | {"hold_ms": 3000})
    assert "hold" not in describe_input(SWIPE | {"hold_ms": 0})


def test_a_person_taking_over_may_hold_a_point() -> None:
    assert "long_press" in rules.MANUAL_INPUT_KINDS
    assert "double_tap" not in rules.MANUAL_INPUT_KINDS


@pytest.fixture
async def rig(db: Database, admin_db: Database, tmp_path: Path) -> Rig:
    tenant = await provision(admin_db, "acme", devices=1)
    rig = Rig(db, tenant, tmp_path)
    capabilities = ["tap", "swipe", "long_press", "double_tap", "launch_app"]
    await rig.connect([device_info(0) | {"capabilities": capabilities}])
    return rig


async def test_a_held_swipe_is_checked_for_risk_and_reaches_the_phone_with_its_timing(
    rig: Rig,
) -> None:
    device = rig.tenant.device_ids[0]
    rig.model.script = [
        lambda r: [
            call(
                "swipe",
                **input_args(
                    device_id=device,
                    observation_id=last_screenshot(r),
                    x1=150,
                    y1=2000,
                    x2=80,
                    y2=2000,
                    duration_ms=150,
                    hold_ms=3000,
                ),
            )
        ],
        [call("ask_user", question="Is that far enough?")],
    ]
    task = await rig.create_task("Walk the character to the left")
    await rig.settle()
    (action,) = await rig.actions(task.id)
    assert action.status == ActionStatus.DONE
    assert rig.risk.calls[0]["payload"]["hold_ms"] == 3000
    (sent,) = rig.gateway.actions
    assert sent.input.model_dump() == {
        "kind": "swipe",
        "x1": 150.0,
        "y1": 2000.0,
        "x2": 80.0,
        "y2": 2000.0,
        "duration_ms": 150,
        "hold_ms": 3000,
        "path": None,  # only a person's drag has one
    }


def test_the_agent_can_zoom_out_and_in_with_a_pinch() -> None:
    assert {"x", "y", "scale"} <= set(offered("pinch")["parameters"]["required"])
    pinch = tools.parse_input("pinch", input_args(x=540, y=1200, scale=0.25))
    assert pinch.payload == {"kind": "pinch", "x": 540.0, "y": 1200.0, "scale": 0.25}
    assert "zoom out 0.25 times" in describe_input(pinch.payload)
    assert "zoom in 3 times" in describe_input(pinch.payload | {"scale": 3})


def test_a_pinch_is_judged_like_a_swipe_and_must_be_on_the_screenshot() -> None:
    assert "pinch" in rules.ASSESSED_KINDS
    assert "pinch" not in rules.MANUAL_INPUT_KINDS  # the web has no two-finger input
    device = replace(DEVICE, capabilities=["pinch"])
    with pytest.raises(Rejected, match="out_of_bounds"):
        admit(device=device, payload={"kind": "pinch", "x": 10.0, "y": 2400.0, "scale": 2.0})
