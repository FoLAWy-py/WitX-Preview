"""Device adapters against a mock Appium server (XCUITest on iOS, UiAutomator2 on Android)."""

import asyncio
import base64
import io
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import witx_gateway.devices.appium as driver
import witx_gateway.devices.ios as ios_module
from PIL import Image
from pydantic import ValidationError
from witx_contracts.common import Swipe
from witx_gateway import apps, ports
from witx_gateway.devices.android import AndroidDevice
from witx_gateway.devices.base import (
    DeviceError,
    DeviceState,
    WrongField,
    pinch_fingers,
    same_screen,
    solid_png,
)
from witx_gateway.devices.ios import IOSDevice
from witx_gateway.journal import Journal

Requests = list[tuple[str, str, object]]


def appium(requests: Requests, world: dict[str, Any] | None = None) -> httpx.MockTransport:
    """A mock Appium server. ``world`` may change between calls to change the device."""
    world = world if world is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        path = request.url.path
        ok = lambda value: httpx.Response(200, json={"value": value})  # noqa: E731
        if path.startswith("/session/GONE/") or any(
            path.startswith(f"/session/{gone}/") for gone in world.get("gone", ())
        ):
            return httpx.Response(404, json={"value": {"error": "invalid session id"}})
        if path.startswith("/session/DEAD/"):  # the on-device agent died
            return httpx.Response(
                500,
                json={
                    "value": {
                        "error": "unknown error",
                        "message": "'GET /window/current/size' cannot be proxied to "
                        "UiAutomator2 server because the instrumentation process is not "
                        "running (probably crashed).",
                    }
                },
            )
        if path == "/session/DEAD" and request.method == "DELETE":
            return ok(None)
        if path == "/session":
            queued = world.get("sessions") or []  # sessions the next opens return
            return ok({"sessionId": queued.pop(0) if queued else "S1", "capabilities": {}})
        if path.endswith("/appium/settings"):
            world.setdefault("settings", {}).update(body["settings"])  # type: ignore[index]
            return ok(None)
        if path.endswith("/execute/sync"):
            script = body["script"]  # type: ignore[index]
            args = (body["args"] or [{}])[0]  # type: ignore[index]
            if script == "mobile: activeAppInfo" and world.get("busy_screen"):
                # A screen with tens of thousands of elements: WebDriverAgent answers only
                # when it need not snapshot the whole tree; "hopeless", never.
                depth = world.get("settings", {}).get("snapshotMaxDepth", 50)
                if depth > 1 or world["busy_screen"] == "hopeless":
                    raise httpx.ReadTimeout("snapshotting the element tree", request=request)
            if script == "mobile: setClipboard":
                world["clipboard"] = base64.b64decode(args["content"]).decode()
            if script == "mobile: pressKey" and args == {"keycode": 29, "metastate": 0x3000}:
                world["selected"] = True  # Ctrl+A: what goes in next replaces the field's text
            if script == "mobile: pressKey" and args["keycode"] == 279 and world.get("paste", True):
                kept = "" if world.pop("selected", False) else world.get("field_text", "")
                world["field_text"] = kept + world.get("clipboard", "")
            if script == "mobile: type":
                kept = "" if world.pop("selected", False) else world.get("field_text", "")
                world["field_text"] = kept + args["text"]
            screen_size = world.get("size", (402, 874))
            return ok(
                {
                    "mobile: isLocked": world.get("locked", False),
                    "mobile: activeAppInfo": {"bundleId": "com.apple.Preferences"},
                    "mobile: deviceScreenInfo": {
                        "screenSize": dict(zip(("width", "height"), screen_size, strict=True)),
                        "statusBarSize": {"width": 402, "height": 54},
                        "scale": 3,
                    },
                    "mobile: getCurrentPackage": "com.android.settings",
                    "mobile: isAppInstalled": world.get("installed", True),
                    "mobile: listApps": world.get(
                        "apps", {"com.huawei.calculator": {"packageName": "x"}}
                    ),
                    "mobile: deviceInfo": world.get(
                        "device_info",
                        {
                            "isSimulator": False,
                            "lockdownInfo": {"UniqueDeviceID": "UDID-1"},
                            "androidId": "A1",
                        },
                    ),
                }.get(script)
            )
        if path.endswith("/window/rect"):
            width, height = world.get("size", (402, 874))
            return ok({"x": 0, "y": 0, "width": width, "height": height})
        if path.endswith("/rotation"):
            return ok({"x": 0, "y": 0, "z": world.get("rotation", 0)})
        if path.endswith("/element/active"):
            if world.get("no_focus"):
                return httpx.Response(404, json={"value": {"error": "no such element"}})
            return ok({"element-6066-11e4-a52e-4f735466cecf": "E1"})
        if path.endswith("/element/E1/attribute/type"):
            return ok(world.get("field_type", "XCUIElementTypeTextField"))
        if path.endswith("/element/E1/attribute/password"):
            return ok(world.get("password", "false"))
        if path.endswith("/element/E1/rect"):
            return ok(world.get("field_rect", {"x": 144, "y": 2125, "width": 992, "height": 192}))
        if path.rsplit("/", 1)[-1] in (
            "content-desc",
            "hint",
            "resource-id",
            "label",
            "name",
            "placeholderValue",
        ):
            return ok(world.get("names", {}).get(path.rsplit("/", 1)[-1]))
        if path.endswith("/element/E1/clear"):
            world["field_text"] = ""
            return ok(None)
        if path.endswith("/element/E1/text"):
            return ok(world.get("field_text", ""))
        if path.endswith("/element/E1/attribute/value"):
            return ok(world.get("field_text", ""))
        if path.endswith("/elements") and body == {
            "using": "class name",
            "value": "XCUIElementTypePickerWheel",
        }:
            return ok([{"element-6066-11e4-a52e-4f735466cecf": w} for w in ("HOUR", "MINUTE")])
        if path.endswith("/elements") and body == {
            "using": "class name",
            "value": "XCUIElementTypeSlider",
        }:
            return ok(
                [{"element-6066-11e4-a52e-4f735466cecf": "SLIDER"}] if "slider" in world else []
            )
        if path.endswith("/element/SLIDER/rect"):
            return ok({"x": 36, "y": 750, "width": 330, "height": 65})
        if path.endswith("/element/SLIDER/attribute/value"):
            return ok(world["slider"])
        if path.endswith("/element/SLIDER/attribute/visible"):
            return ok("true")
        if path.endswith("/element/SLIDER/value"):
            world["slider"] = f"{world['slider_lands'].pop(0)}%"  # where the slider came to rest
            return ok(None)
        if path.endswith("/element/HOUR/rect"):
            return ok({"x": 111, "y": 278, "width": 55, "height": 293})
        if path.endswith("/element/MINUTE/rect"):
            return ok({"x": 171, "y": 278, "width": 50, "height": 293})
        if path.endswith("/source"):
            return ok(world["source"]())
        if path.endswith("/actions") and world.get("on_actions"):
            world["on_actions"](body)
            return ok(None)
        if path.endswith("/value") or path.endswith("/actions"):
            return ok(None)
        if path.endswith("/screenshot") and world.get("unauthorized"):
            return httpx.Response(
                500,
                json={
                    "value": {
                        "error": "unable to capture screen",
                        "message": "Error Domain=XCTDaemonErrorDomain Code=41 "
                        '"Not authorized for performing UI testing actions."',
                    }
                },
            )
        if path.endswith("/screenshot"):
            png = world.get("screenshot") or solid_png(1206, 2622)
            return ok(base64.b64encode(png).decode())
        return httpx.Response(404, json={"value": {"error": "unknown command"}})

    return httpx.MockTransport(handler)


def android(
    requests: Requests, world: dict[str, Any] | None = None, journal: Journal | None = None
) -> AndroidDevice:
    return AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
        identities=journal,
    )


def scripts(requests: Requests) -> list[object]:
    return [body for _, path, body in requests if path.endswith("/execute/sync")]


async def test_android_state_reads_geometry_and_foreground_app() -> None:
    device = android([], {"size": (2856, 1280), "rotation": 270})
    assert await device.state() == DeviceState(2856, 1280, 270, "com.android.settings")


async def test_a_screen_turned_an_unknown_way_has_no_geometry() -> None:
    device = android([], {"size": (2856, 1280), "rotation": 45})
    with pytest.raises(DeviceError) as error:
        await device.state()
    assert error.value.reason == "unknown_geometry"


async def test_android_waits_at_most_3_s_for_a_settled_screen() -> None:
    requests: Requests = []
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests),
    )
    await device.state()
    caps = requests[0][2]["capabilities"]["alwaysMatch"]  # type: ignore[index]
    assert caps["appium:automationName"] == "UiAutomator2"
    assert caps["appium:waitForIdleTimeout"] == 3000


async def test_android_locked_screen_is_refused() -> None:
    with pytest.raises(DeviceError, match="locked"):
        await android([], {"locked": True}).state()


def slow_commands(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "slow driver command"]


async def test_a_slow_driver_command_is_logged_with_the_last_foreground_app_but_never_its_arguments(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    device = android([])
    await device.state()  # reads the foreground app
    await device.check_text_target()
    monkeypatch.setattr(driver, "SLOW_CALL", 0.0)  # every command counts as slow
    with caplog.at_level(logging.WARNING, logger=driver.__name__):
        await device.type_text("买牛奶 WitX")
    logged = slow_commands(caplog)
    assert {r.command for r in logged} >= {"mobile: setClipboard", "mobile: pressKey"}  # type: ignore[attr-defined]
    assert {r.foreground_app for r in logged} == {"com.android.settings"}  # type: ignore[attr-defined]
    assert {r.outcome for r in logged} == {"answered"}  # type: ignore[attr-defined]
    assert all(isinstance(r.duration_ms, int) for r in logged)  # type: ignore[attr-defined]
    pasted = base64.b64encode("买牛奶 WitX".encode()).decode()
    assert not any("买牛奶" in str(vars(r)) or pasted in str(vars(r)) for r in logged)


async def test_a_driver_command_that_times_out_is_logged_however_soon(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=httpx.MockTransport(no_answer),
    )
    with (
        caplog.at_level(logging.WARNING, logger=driver.__name__),
        pytest.raises(DeviceError, match="timeout"),
    ):
        await device.state()
    [record] = slow_commands(caplog)
    assert (record.command, record.outcome, record.foreground_app) == (  # type: ignore[attr-defined]
        "POST /session",
        "timeout",
        None,
    )


async def test_a_quick_driver_command_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=driver.__name__):
        await android([]).state()
    assert slow_commands(caplog) == []


async def test_android_pastes_unicode_at_the_cursor_and_leaves_nothing_on_the_clipboard() -> None:
    requests: Requests = []
    world: dict[str, Any] = {}
    device = android(requests, world)
    await device.check_text_target()
    await device.type_text("买牛奶 WitX")
    sent = [(s["script"], s["args"][0]) for s in scripts(requests)]  # type: ignore[index]
    assert ("mobile: pressKey", {"keycode": 279}) in sent
    assert not any(script == "mobile: type" for script, _ in sent)  # pasted whole, not key by key
    assert world["field_text"] == "买牛奶 WitX"
    assert world["clipboard"] == ""


async def test_android_typing_into_a_field_gone_before_the_paste_says_so_and_sends_nothing() -> (
    None
):
    requests: Requests = []
    world: dict[str, Any] = {}
    device = android(requests, world)
    await device.check_text_target()
    world["no_focus"] = True  # the field closed after the check
    assert await device.type_text("Ann", replace=True) == "no_focused_field"
    assert "clipboard" not in world
    assert [s for s in scripts(requests) if s["script"] == "mobile: pressKey"] == []  # type: ignore[index]


async def test_android_typing_that_fails_once_the_paste_began_still_raises() -> None:
    base = appium([], {})

    def handler(request: httpx.Request) -> httpx.Response:
        if b"pressKey" in request.content:  # the session is lost while pasting
            return httpx.Response(404, json={"value": {"error": "invalid session id"}})
        return base.handler(request)  # type: ignore[no-any-return]

    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(DeviceError, match="session_lost"):
        await device.type_text("Ann")


async def test_android_types_into_a_field_that_refuses_paste_without_doubling_text() -> None:
    world: dict[str, Any] = {"paste": False, "field_text": "Name"}
    device = android([], world)
    await device.type_text("Ann")
    assert world["field_text"] == "NameAnn"  # typed once, after the refused paste
    world.update(paste=True, field_text="Name")
    await device.type_text("Ann")
    assert world["field_text"] == "NameAnn"  # pasted once, no typing after it


async def test_android_does_not_type_again_over_text_the_field_already_shows() -> None:
    world: dict[str, Any] = {"paste": False, "field_text": "Ann"}
    await android([], world).type_text("Ann")
    assert world["field_text"] == "Ann"  # cannot tell a refused paste from a same-text paste


async def test_android_selects_the_field_text_so_that_the_paste_replaces_it() -> None:
    world: dict[str, Any] = {"field_text": "Ann Lee"}
    device = android([], world)
    await device.type_text("安娜", replace=True)
    assert world["field_text"] == "安娜"
    await device.type_text(" Li")
    assert world["field_text"] == "安娜 Li"  # without replace it goes in next to the text


async def test_android_types_over_the_selection_once_when_a_field_refuses_paste() -> None:
    world: dict[str, Any] = {"paste": False, "field_text": "Ann Lee"}
    await android([], world).type_text("Ann", replace=True)
    assert world["field_text"] == "Ann"  # "Ann" was in the old text, yet it was replaced


async def test_android_reads_back_the_focused_field_but_never_a_password_field() -> None:
    world: dict[str, Any] = {
        "field_text": "+86 137 0013 0810",
        "names": {"hint": "Phone", "resource-id": "com.android:id/phone"},
    }
    device = android([], world)
    read = await device.read_text_field()
    assert (read.text, read.name) == ("+86 137 0013 0810", "Phone")  # hint before resource-id
    assert read.box == (144.0, 2125.0, 992.0, 192.0)
    world["password"] = "true"
    with pytest.raises(DeviceError, match="secure_field"):
        await device.read_text_field()


async def test_the_typed_text_is_taken_out_of_a_description_that_holds_it() -> None:
    # Compose describes a field as "<text>, <name>", and the hint is gone once text is in.
    world: dict[str, Any] = {
        "field_text": "测试1030, First name",  # the field's text carries the label too
        "names": {"hint": "null", "content-desc": "测试1030, First name"},
    }
    assert (await android([], world).read_text_field("测试1030")).name == "First name"
    world["names"] = {"content-desc": "测试1030"}  # nothing but the text: no name
    assert (await android([], world).read_text_field("测试1030")).name == ""


async def test_a_field_with_no_name_is_read_back_with_an_empty_one() -> None:
    nothing = {"hint": "null", "resource-id": "null"}  # what UiAutomator2 says for no value
    read = await android([], {"field_text": "+1", "names": nothing}).read_text_field()
    assert (read.text, read.name) == ("+1", "")


async def test_ios_reads_back_the_focused_field_but_never_a_secure_text_field() -> None:
    world: dict[str, Any] = {"field_text": "Ann", "names": {"label": "First name"}}
    device = IOSDevice(
        udid="UDID-1", name="n", appium_url="http://127.0.0.1:4723", transport=appium([], world)
    )
    read = await device.read_text_field()
    assert (read.text, read.name) == ("Ann", "First name")
    world["field_type"] = "XCUIElementTypeSecureTextField"
    with pytest.raises(DeviceError, match="secure_field"):
        await device.read_text_field()


async def test_android_refuses_to_type_into_a_password_field() -> None:
    with pytest.raises(DeviceError, match="secure_field"):
        await android([], {"password": "true"}).check_text_target()


async def test_android_does_not_type_into_a_password_field_the_focus_moved_to_after_the_check() -> (
    None
):
    world: dict[str, Any] = {}
    device = android([], world)
    await device.check_text_target()
    world["password"] = "true"  # the form moved on to its password field
    assert await device.type_text("hunter2") == "secure_field"
    assert "hunter2" not in (world.get("field_text", ""), world.get("clipboard", ""))


async def test_a_person_taking_an_android_phone_over_types_into_a_password_field() -> None:
    world: dict[str, Any] = {"password": "true"}
    assert await android([], world).type_text("hunter2", person=True) is None
    assert world["field_text"] == "hunter2"


async def test_android_refuses_to_type_on_a_screen_hidden_from_screenshots() -> None:
    black = solid_png(1280, 2856, grey=0)  # what FLAG_SECURE windows look like
    with pytest.raises(DeviceError, match="secure_field"):
        await android([], {"screenshot": black}).check_text_target()


async def test_android_inputs_use_gestures_and_key_codes() -> None:
    requests: Requests = []
    device = android(requests)
    await device.tap(10.7, 20.2)
    await device.back()
    await device.home()
    await device.launch_app("com.android.settings")
    assert scripts(requests) == [
        {"script": "mobile: clickGesture", "args": [{"x": 10.7, "y": 20.2}]},
        {"script": "mobile: pressKey", "args": [{"keycode": 4}]},
        {"script": "mobile: pressKey", "args": [{"keycode": 3}]},
        {"script": "mobile: activateApp", "args": [{"appId": "com.android.settings"}]},
    ]
    with pytest.raises(DeviceError, match="invalid_app_id"):
        await device.launch_app("com.x; reboot")


async def test_android_side_keys_are_key_codes_and_locking_is_the_drivers_lock() -> None:
    requests: Requests = []
    device = android(requests)
    await device.volume_up()
    await device.volume_down()
    await device.lock()
    assert scripts(requests) == [
        {"script": "mobile: pressKey", "args": [{"keycode": 24}]},
        {"script": "mobile: pressKey", "args": [{"keycode": 25}]},
        {"script": "mobile: lock", "args": []},
    ]
    assert {"volume_up", "volume_down", "lock"} <= set(device.capabilities)


FINGER = {"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}}
DOWN = {"type": "pointerDown", "button": 0}
UP = {"type": "pointerUp", "button": 0}


def at(x: int, y: int, duration: int = 0) -> dict[str, Any]:
    return {"type": "pointerMove", "duration": duration, "x": x, "y": y}


def wait(duration: int) -> dict[str, Any]:
    return {"type": "pause", "duration": duration}


def gestures(requests: Requests) -> list[Any]:
    """The finger's steps of each W3C action sent."""
    found = [body for _, path, body in requests if path.endswith("/actions")]
    assert all(b["actions"][0] | {"actions": None} == FINGER | {"actions": None} for b in found)  # type: ignore[index]
    return [b["actions"][0]["actions"] for b in found]  # type: ignore[index]


def phone(platform: str, requests: Requests) -> AndroidDevice | IOSDevice:
    return android(requests) if platform == "android" else ios_device("UDID-1", {}, requests)


@pytest.mark.parametrize("platform", ["android", "ios"])
async def test_a_swipe_moves_the_finger_for_its_duration_and_lets_go_at_the_end(
    platform: str,
) -> None:
    # On iOS too: XCUITest's own drag would press for the duration first, then drag.
    requests: Requests = []
    await phone(platform, requests).swipe(100.4, 800, 100.4, 200.6, 300)
    assert gestures(requests) == [[at(100, 800), DOWN, at(100, 201, 300), UP]]
    assert not scripts(requests)  # no mobile: dragFromToForDuration


@pytest.mark.parametrize("platform", ["android", "ios"])
async def test_a_held_swipe_stays_pressed_at_the_end_before_letting_go(platform: str) -> None:
    requests: Requests = []
    await phone(platform, requests).swipe(80, 700, 80, 600, 200, hold_ms=3000)
    assert gestures(requests) == [[at(80, 700), DOWN, at(80, 600, 200), wait(3000), UP]]


@pytest.mark.parametrize("platform", ["android", "ios"])
async def test_a_swipe_with_a_path_moves_the_finger_from_point_to_point_in_their_times(
    platform: str,
) -> None:
    # A person's drag: each move takes the time between its point and the one before;
    # a pause in the drag (two points with the same place) is a move that goes nowhere.
    requests: Requests = []
    path = [(100.0, 800.0, 0), (120.4, 790.0, 48), (120.4, 790.0, 200), (300.0, 500.6, 420)]
    await phone(platform, requests).swipe(100, 800, 300, 500.6, 420, hold_ms=150, path=path)
    assert gestures(requests) == [
        [
            at(100, 800),
            DOWN,
            at(120, 790, 48),
            at(120, 790, 152),
            at(300, 501, 220),
            wait(150),
            UP,
        ]
    ]


@pytest.mark.parametrize(
    "path",
    [
        [{"x": 0, "y": 0, "t_ms": t} for t in range(0, 201)] + [{"x": 9, "y": 9, "t_ms": 300}],
        [{"x": 0, "y": 0, "t_ms": 0}, {"x": 5, "y": 5, "t_ms": 200}, {"x": 9, "y": 9, "t_ms": 300}][
            ::-1
        ],
        [{"x": 0, "y": 0, "t_ms": 0}, {"x": 5, "y": 5, "t_ms": 250}, {"x": 9, "y": 9, "t_ms": 200}],
        [{"x": 0, "y": 0, "t_ms": 0}, {"x": 9, "y": 9, "t_ms": 299}],  # ends before duration_ms
        [{"x": 1, "y": 0, "t_ms": 0}, {"x": 9, "y": 9, "t_ms": 300}],  # starts off (x1, y1)
        [{"x": 0, "y": 0, "t_ms": 0}],
    ],
)
def test_a_path_with_too_many_points_or_out_of_time_order_is_not_a_valid_swipe(
    path: list[dict[str, int]],
) -> None:
    swipe = {"kind": "swipe", "x1": 0, "y1": 0, "x2": 9, "y2": 9, "duration_ms": 300}
    with pytest.raises(ValidationError):
        Swipe.model_validate(swipe | {"path": path})
    assert Swipe.model_validate(swipe).path is None  # without one, a swipe is as before


@pytest.mark.parametrize("platform", ["android", "ios"])
async def test_a_long_press_holds_one_point_without_moving(platform: str) -> None:
    requests: Requests = []
    await phone(platform, requests).long_press(150.2, 320.7, 1200)
    assert gestures(requests) == [[at(150, 321), DOWN, wait(1200), UP]]


@pytest.mark.parametrize("platform", ["android", "ios"])
async def test_a_double_tap_is_two_short_presses_on_one_point(platform: str) -> None:
    requests: Requests = []
    await phone(platform, requests).double_tap(150, 320)
    press = [DOWN, wait(50), UP]
    assert gestures(requests) == [[at(150, 320), *press, wait(100), *press]]


@pytest.mark.parametrize("platform", ["android", "ios"])
async def test_a_pinch_moves_two_fingers_in_short_steps_and_rests_before_lifting(
    platform: str,
) -> None:
    requests: Requests = []
    await phone(platform, requests).pinch(200, 400, 160, 40)
    (sent,) = [body for _, path, body in requests if path.endswith("/actions")]
    left, right = sent["actions"]  # type: ignore[index]
    assert (left["id"], right["id"]) == ("finger", "finger1")  # two fingers at once
    for finger, side in ((left, -1), (right, 1)):
        steps = finger["actions"]
        assert steps[:2] == [at(200 + side * 160, 400), DOWN]
        moves = steps[2:-2]
        # Many short moves: one long move of two fingers is played as a jump on Android.
        assert {m["duration"] for m in moves} == {25} and len(moves) == 16
        assert moves[-1] == at(200 + side * 40, 400, 25)
        assert steps[-2:] == [wait(400), UP]  # it rests, so that the map does not zoom on


async def test_ios_creates_a_session_and_reads_state() -> None:
    requests: Requests = []
    device = IOSDevice(
        udid="UDID-1",
        name="iPhone",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests),
    )
    state = await device.state()
    assert state == DeviceState(402, 874, 0, "com.apple.Preferences")
    caps = requests[0][2]["capabilities"]["alwaysMatch"]  # type: ignore[index]
    assert caps["appium:udid"] == "UDID-1" and caps["appium:automationName"] == "XCUITest"
    assert caps["appium:settings[animationCoolOffTimeout]"] == 0  # no wait for idle
    assert len(await device.screenshot()) > 0
    await device.tap(100, 200)
    assert gestures(requests)[-1] == [at(100, 200), DOWN, wait(50), UP]


def settings_and_scripts(requests: Requests) -> list[Any]:
    """What was asked of the driver, in order: a settings change, a script's name or a
    W3C gesture."""
    found: list[Any] = []
    for _, path, body in requests:
        if path.endswith("/appium/settings"):
            found.append(body["settings"])  # type: ignore[index]
        elif path.endswith("/execute/sync"):
            found.append(body["script"])  # type: ignore[index]
        elif path.endswith("/actions"):
            found.append("POST /actions")
    return found


async def test_ios_gestures_and_taps_run_with_the_element_tree_cut_to_the_apps_root() -> None:
    requests: Requests = []
    device = ios_device("UDID-1", {}, requests)
    await device.swipe(10, 700, 10, 300, 800, path=[(10, 700, 0), (10, 500, 400), (10, 300, 800)])
    await device.tap(100, 200)
    await device.long_press(100, 200, 600)
    root_only, whole = {"snapshotMaxDepth": 1}, {"snapshotMaxDepth": 50}
    assert settings_and_scripts(requests) == [root_only, "POST /actions", whole] * 3


async def test_a_slow_settings_change_is_logged_with_what_it_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(driver, "SLOW_CALL", 0.0)
    with caplog.at_level(logging.WARNING, logger=driver.__name__):
        await ios_device("UDID-1", {}).state()
    logged = [r for r in slow_commands(caplog) if r.command == "POST /appium/settings"]  # type: ignore[attr-defined]
    assert [r.snapshotMaxDepth for r in logged] == [1, 50]  # type: ignore[attr-defined]


async def test_a_slow_gesture_is_logged_with_its_steps_and_planned_time_but_never_its_points(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    device = ios_device("UDID-1", {})
    monkeypatch.setattr(driver, "SLOW_CALL", 0.0)
    with caplog.at_level(logging.WARNING, logger=driver.__name__):
        await device.swipe(
            10, 700, 10, 300, 800, path=[(10, 700, 0), (10, 500, 300), (10, 300, 800)]
        )
    [record] = [r for r in slow_commands(caplog) if r.command == "POST /actions"]  # type: ignore[attr-defined]
    assert (record.steps, record.planned_ms) == (5, 800)  # type: ignore[attr-defined]
    # What the log line carries beyond any record's own fields (its time, thread, ...).
    standard = vars(logging.makeLogRecord({}))
    logged = {k: v for k, v in vars(record).items() if k not in standard}
    assert "700" not in str(logged)


async def test_ios_reads_the_foreground_app_of_a_busy_screen_without_its_element_tree() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"busy_screen": True, "size": (874, 402), "rotation": 90}
    state = await ios_device("UDID-1", world, requests).state()
    assert state == DeviceState(874, 402, 90, "com.apple.Preferences")
    asked = settings_and_scripts(requests)
    app = asked.index("mobile: activeAppInfo")
    assert asked[app - 1 : app + 2] == [
        {"snapshotMaxDepth": 1},
        "mobile: activeAppInfo",
        {"snapshotMaxDepth": 50},
    ]
    assert not any(path.endswith("/window/rect") for _, path, _ in requests)


def screen_reads(requests: Requests) -> int:
    return settings_and_scripts(requests).count("mobile: deviceScreenInfo")


async def test_ios_reads_the_screen_size_once_per_session_and_turns_it_with_the_phone() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"size": (402, 874)}
    device = ios_device("UDID-1", world, requests)
    assert (await device.state()) == DeviceState(402, 874, 0, "com.apple.Preferences")
    world["rotation"] = 90
    assert (await device.state()) == DeviceState(874, 402, 90, "com.apple.Preferences")
    world["rotation"] = 180
    assert (await device.state()) == DeviceState(402, 874, 180, "com.apple.Preferences")
    assert screen_reads(requests) == 1


async def test_ios_reads_the_screen_size_again_in_a_new_session() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"size": (402, 874), "sessions": ["S1", "S2"]}
    device = ios_device("UDID-1", world, requests)
    await device.state()
    world["gone"] = {"S1"}  # the driver restarted; the phone's display zoom may have changed
    world["size"] = (375, 812)
    with pytest.raises(DeviceError, match="session_lost"):
        await device.state()
    assert (await device.state()).input_width == 375
    assert screen_reads(requests) == 2


async def test_ios_lets_the_element_tree_out_again_when_the_foreground_app_cannot_be_read() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"busy_screen": "hopeless"}
    with pytest.raises(DeviceError, match="timeout"):
        await ios_device("UDID-1", world, requests).state()
    asked = settings_and_scripts(requests)
    assert asked[-2:] == ["mobile: activeAppInfo", {"snapshotMaxDepth": 50}]
    assert world["settings"] == {"snapshotMaxDepth": 50}


def test_ios_refuses_a_remote_appium_server() -> None:
    with pytest.raises(ValueError):
        IOSDevice(udid="U", name="n", appium_url="http://10.0.0.5:4723")


async def test_ios_refuses_to_type_into_a_secure_text_field() -> None:
    world: dict[str, Any] = {}
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium([], world),
    )
    await device.check_text_target()
    world["field_type"] = "XCUIElementTypeSecureTextField"
    with pytest.raises(DeviceError, match="secure_field"):
        await device.check_text_target()


async def test_ios_does_not_set_text_into_a_secure_field_the_focus_moved_to_after_the_check() -> (
    None
):
    requests: Requests = []
    world: dict[str, Any] = {}
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    await device.check_text_target()
    world["field_type"] = "XCUIElementTypeSecureTextField"  # the form moved on
    assert await device.type_text("hunter2") == "secure_field"
    assert "hunter2" not in str(requests)


async def test_a_person_taking_an_iphone_over_types_into_a_secure_field() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"field_type": "XCUIElementTypeSecureTextField"}
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    assert await device.type_text("hunter2", person=True) is None
    assert "hunter2" in str(requests)


async def test_ios_sets_the_picker_wheel_under_the_point() -> None:
    requests: Requests = []
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests),
    )
    await device.set_picker(190, 420, "30")
    assert requests[-1] == ("POST", "/session/S1/element/MINUTE/value", {"text": "30"})
    with pytest.raises(DeviceError, match="no_picker"):
        await device.check_picker_target(300, 420)


async def test_ios_setting_text_into_a_field_gone_before_any_key_says_so_and_sends_nothing() -> (
    None
):
    requests: Requests = []
    world: dict[str, Any] = {}
    device = ios_device("UDID-1", world, requests)
    await device.check_text_target()
    world["no_focus"] = True  # the field closed after the check
    assert await device.type_text("Ann", replace=True) == "no_focused_field"
    assert [p for method, p, _ in requests if method == "POST" and "/element/" in p] == []


async def test_ios_picker_wheel_gone_before_it_is_turned_says_so_and_turns_nothing() -> None:
    requests: Requests = []
    device = ios_device("UDID-1", {}, requests)
    assert await device.set_picker(300, 420, "30") == "no_picker"  # no wheel under the point
    assert not [p for _, p, _ in requests if p.endswith("/value")]


async def test_ios_sets_a_slider_again_until_it_reports_the_value() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"slider": "9%", "slider_lands": [18, 36, 27]}  # two near misses
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    await device.check_picker_target(200, 770)  # on the slider
    await device.set_picker(200, 770, "27%")
    assert world["slider"] == "27%"
    positions = [r[2]["text"] for r in requests if r[1].endswith("/element/SLIDER/value")]  # type: ignore[index]
    assert len(set(positions)) == 3  # three tries, each aimed a little differently


async def test_ios_gives_up_on_a_slider_after_a_few_tries_and_leaves_it_where_it_is() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"slider": "9%", "slider_lands": [18] * 10}
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    await device.set_picker(200, 770, "27%")
    assert world["slider"] == "18%"
    assert len([r for r in requests if r[1].endswith("/element/SLIDER/value")]) == 6


async def test_ios_does_not_touch_a_slider_for_a_value_that_is_not_a_percentage() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"slider": "9%", "slider_lands": [18]}
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    await device.set_picker(200, 770, "large")
    assert world["slider"] == "9%"


async def test_ios_reads_the_sliders_in_view() -> None:
    world: dict[str, Any] = {"slider": "27%", "slider_lands": []}
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium([], world),
    )
    (slider,) = await device.read_sliders()
    assert (slider.value, slider.center) == ("27%", (201.0, 782.5))
    del world["slider"]
    assert await device.read_sliders() == []


async def test_ios_opens_a_new_session_after_the_old_one_is_lost() -> None:
    requests: Requests = []
    device = IOSDevice(
        udid="UDID-1",
        name="n",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, {"sessions": ["GONE"]}),
    )
    with pytest.raises(DeviceError, match="session_lost"):
        await device.state()
    assert (await device.state()).foreground_app == "com.apple.Preferences"
    assert ("POST", "/session") in [(m, p) for m, p, _ in requests]


SIMULATOR = "11111111-1111-4111-8111-111111111111"


def ios_device(udid: str, world: dict[str, Any], requests: Requests | None = None) -> IOSDevice:
    return IOSDevice(
        udid=udid,
        name="iPhone",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests if requests is not None else [], world),
    )


async def test_ios_empties_the_field_before_typing_text_that_replaces_it() -> None:
    requests: Requests = []
    device = ios_device(SIMULATOR, {"device_info": {"isSimulator": True}}, requests)
    await device.type_text("Ann", replace=True)
    await device.type_text("Lee")
    sent = [p.rsplit("/", 1)[-1] for method, p, _ in requests if "/E1/" in p and method == "POST"]
    assert sent == ["clear", "value", "value"]


async def test_ios_refuses_a_session_that_reaches_a_real_phone_instead_of_the_simulator() -> None:
    # Two WebDriverAgents on one local port: the simulator's session answered by a phone.
    world = {
        "device_info": {
            "isSimulator": False,
            "lockdownInfo": {"UniqueDeviceID": "00000000-0000000000000001"},
        }
    }
    with pytest.raises(DeviceError, match="wrong_device"):
        await ios_device(SIMULATOR, world).state()


async def test_ios_refuses_a_real_phone_whose_udid_is_not_the_configured_one() -> None:
    world = {"device_info": {"isSimulator": False, "lockdownInfo": {"UniqueDeviceID": "OTHER"}}}
    with pytest.raises(DeviceError, match="wrong_device"):
        await ios_device("UDID-1", world).state()


async def test_a_device_that_changes_under_a_session_is_refused() -> None:
    phone: dict[str, Any] = {"device_info": {"androidId": "A1"}}
    pixel = android([], phone)
    await pixel.state()
    phone["device_info"] = {"androidId": "A2"}
    with pytest.raises(DeviceError, match="wrong_device"):
        await pixel.state()


async def test_the_device_first_seen_is_kept_across_a_gateway_restart(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    phone: dict[str, Any] = {"device_info": {"androidId": "A1"}}
    await android([], phone, journal).state()
    # The Gateway restarts; another emulator now runs as emulator-5554.
    phone["device_info"] = {"androidId": "A2"}
    restarted = android([], phone, journal)
    with pytest.raises(DeviceError, match="wrong_device"):
        await restarted.state()
    assert restarted.identity_changed  # reported to the cloud: the device takes no tasks


async def test_a_person_accepts_a_new_device_by_forgetting_the_old_one(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    phone: dict[str, Any] = {"device_info": {"androidId": "A1"}}
    await android([], phone, journal).state()
    phone["device_info"] = {"androidId": "A2"}
    with pytest.raises(DeviceError, match="wrong_device"):
        await android([], phone, journal).state()
    assert journal.forget_identity("emulator-5554")
    accepted = android([], phone, journal)
    await accepted.state()
    assert not accepted.identity_changed and journal.pinned_identity("emulator-5554") == "A2"


def ios_simulator(world: dict[str, Any], journal: Journal) -> IOSDevice:
    return IOSDevice(
        udid=SIMULATOR,
        name="iPhone 18 Pro",
        appium_url="http://127.0.0.1:4723",
        transport=appium([], world),
        identities=journal,
    )


async def test_a_simulator_is_still_itself_after_its_webdriveragent_is_reinstalled(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    world: dict[str, Any] = {"device_info": {"isSimulator": True, "uuid": "04AA6D5A-VENDOR-1"}}
    await ios_simulator(world, journal).state()
    world["device_info"] = {"isSimulator": True, "uuid": "89C3CEE4-VENDOR-2"}  # reinstalled
    again = ios_simulator(world, journal)
    await again.state()
    assert not again.identity_changed


async def test_a_simulator_pinned_by_its_old_vendor_id_is_not_turned_away_after_the_upgrade(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.pin_identity(SIMULATOR, "simulator:04AA6D5A-VENDOR-1")  # what an older Gateway wrote
    world: dict[str, Any] = {"device_info": {"isSimulator": True, "uuid": "89C3CEE4-VENDOR-2"}}
    device = ios_simulator(world, journal)
    await device.state()
    assert not device.identity_changed
    assert journal.pinned_identity(SIMULATOR) == f"simulator:{SIMULATOR}"


async def test_a_real_iphone_that_answers_as_another_device_is_still_refused(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    udid = "00000000-0000000000000001"
    world: dict[str, Any] = {"device_info": {"lockdownInfo": {"UniqueDeviceID": udid}}}

    def phone() -> IOSDevice:
        return IOSDevice(
            udid=udid,
            name="iPhone",
            appium_url="http://127.0.0.1:4723",
            transport=appium([], world),
            identities=journal,
        )

    await phone().state()
    world["device_info"] = {"lockdownInfo": {"UniqueDeviceID": "00000000-OTHER"}}
    with pytest.raises(DeviceError, match="wrong_device"):
        await phone().state()


async def test_each_ios_device_opens_its_session_on_the_ports_it_was_given() -> None:
    requests: Requests = []
    device = IOSDevice(
        udid="UDID-1",
        name="iPhone",
        appium_url="http://127.0.0.1:4723",
        ports={"appium:wdaLocalPort": 8101, "appium:mjpegServerPort": 9101},
        transport=appium(requests),
    )
    await device.state()
    caps = requests[0][2]["capabilities"]["alwaysMatch"]  # type: ignore[index]
    assert (caps["appium:wdaLocalPort"], caps["appium:mjpegServerPort"]) == (8101, 9101)


async def test_a_session_with_a_broadcast_port_asks_for_the_broadcast_settings() -> None:
    requests: Requests = []
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        ports={"appium:systemPort": 8200, "appium:mjpegServerPort": 9200},
        transport=appium(requests),
    )
    await device.state()
    caps = requests[0][2]["capabilities"]["alwaysMatch"]  # type: ignore[index]
    assert caps["appium:settings[mjpegServerFramerate]"] == 30
    assert caps["appium:settings[mjpegScalingFactor]"] == 50


async def test_a_phone_without_a_screen_broadcast_port_has_no_stream() -> None:
    requests: Requests = []
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        ports={"appium:systemPort": 8200},
        transport=appium(requests),
    )
    assert await device.stream_frame(after=0, wait=0.1) is None
    assert requests == []  # no session was opened for it


def test_devices_on_one_appium_never_share_a_port() -> None:
    taken = ports.Ports()
    assert [taken.take(p) for p in ("ios", "android", "ios", "android", "fake")] == [
        {"appium:wdaLocalPort": 8100, "appium:mjpegServerPort": 9100},
        {"appium:systemPort": 8101, "appium:mjpegServerPort": 9101},
        {"appium:wdaLocalPort": 8102, "appium:mjpegServerPort": 9102},
        {"appium:systemPort": 8103, "appium:mjpegServerPort": 9103},
        None,
    ]


async def test_on_shutdown_the_gateway_ends_the_session_it_opened() -> None:
    requests: Requests = []
    device = android(requests)
    await device.state()
    await device.close()
    assert ("DELETE", "/session/S1", None) in requests


async def test_a_session_whose_device_agent_died_is_replaced_on_the_next_call() -> None:
    requests: Requests = []
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        ports={"appium:systemPort": 8210},
        transport=appium(requests, {"sessions": ["DEAD"]}),
    )
    with pytest.raises(DeviceError, match="session_lost"):
        await device.state()  # this call fails: nothing is retried behind its back
    assert ("DELETE", "/session/DEAD", None) in requests
    await device.state()  # the next one opens a fresh session, with its agent
    created = next(
        body for method, path, body in requests if (method, path) == ("POST", "/session")
    )
    caps = created["capabilities"]["alwaysMatch"]  # type: ignore[index]
    assert caps["appium:systemPort"] == 8210 and caps["appium:udid"] == "emulator-5554"


@pytest.mark.parametrize("installed", [True, False])
async def test_opening_an_app_first_asks_whether_it_is_installed(installed: bool) -> None:
    requests: Requests = []
    device = android(requests, {"installed": installed})
    if installed:
        await device.check_app("com.huawei.calculator")
    else:
        with pytest.raises(DeviceError, match="app_not_installed"):
            await device.check_app("com.android.calculator2")
    assert scripts(requests)[-1]["script"] == "mobile: isAppInstalled"  # type: ignore[index]


async def test_android_lists_installed_packages_without_names() -> None:
    assert await android([]).list_apps() == {"com.huawei.calculator": None}


async def test_an_iphone_app_is_found_by_its_bundle_name_too() -> None:
    names = {"CFBundleDisplayName": "rednote", "CFBundleName": "小红书"}
    world = {"apps": {"com.xingin.discover": names, "com.apple.Maps": {"CFBundleName": "Maps"}}}
    installed = await ios_device("UDID-1", world).list_apps()
    assert installed == {"com.xingin.discover": "rednote · 小红书", "com.apple.Maps": "Maps"}
    assert apps.match("小红书", installed) == [("com.xingin.discover", "rednote · 小红书")]


def test_screens_are_the_same_when_only_the_clock_or_the_scroll_indicator_differs() -> None:
    def screen(clock: int, offset: int, bar: int = 200) -> bytes:
        image = Image.new("L", (160, 320), 200)
        image.paste(clock, (0, 0, 160, 20))  # the status bar
        image.paste(bar, (155, 100, 160, 260))  # the scroll indicator
        for row in range(40, 320, 40):  # list rows
            image.paste(60, (10, row + offset, 150, row + offset + 12))
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    assert same_screen(screen(10, 0), screen(90, 0))
    assert same_screen(screen(10, 0), screen(10, 0, bar=40))
    assert not same_screen(screen(10, 0), screen(10, 18))  # the list moved on


async def test_android_refuses_text_for_a_field_other_than_the_focused_one() -> None:
    world: dict[str, Any] = {
        "names": {"hint": "Search"},
        "field_rect": {"x": 144, "y": 180, "width": 824, "height": 144},
    }
    device = android([], world)
    await device.check_text_target((500, 250))  # inside it
    with pytest.raises(WrongField) as error:
        await device.check_text_target((500, 1700))
    assert (error.value.reason, error.value.field.name) == ("wrong_field", "Search")
    assert error.value.field.box == (144.0, 180.0, 824.0, 144.0)
    await device.check_text_target(None)  # no point named: nothing to check


async def test_ios_refuses_text_for_a_field_other_than_the_focused_one() -> None:
    world: dict[str, Any] = {
        "names": {"label": "Title"},
        "field_rect": {"x": 43, "y": 155, "width": 295, "height": 29},
    }
    device = IOSDevice(
        udid="UDID-1", name="n", appium_url="http://127.0.0.1:4723", transport=appium([], world)
    )
    await device.check_text_target((150, 170))
    with pytest.raises(WrongField):
        await device.check_text_target((150, 400))


class ScrollingList:
    """A list of countries 8 rows tall; a swipe moves it 5 rows, the way a held swipe does."""

    def __init__(self, names: list[str]) -> None:
        self.names, self.offset = names, 0

    def source(self) -> str:
        rows = "".join(
            f'<android.widget.TextView text="{name}" displayed="true" '
            f'bounds="[100,{100 + 60 * (i - self.offset)}][500,{150 + 60 * (i - self.offset)}]"/>'
            for i, name in enumerate(self.names)
            if self.offset <= i < self.offset + 8
        )
        return f"<hierarchy>{rows}</hierarchy>"

    def swiped(self, body: Any) -> None:
        moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
        up = moves[-1]["y"] < moves[0]["y"]  # the finger goes up: the list goes down
        self.offset = min(max(self.offset + (5 if up else -5), 0), len(self.names) - 8)


def countries(
    names: list[str], world_extra: dict[str, Any] | None = None
) -> tuple[AndroidDevice, ScrollingList, Requests]:
    listing = ScrollingList(names)
    world: dict[str, Any] = {"source": listing.source, "on_actions": listing.swiped} | (
        world_extra or {}
    )
    requests: Requests = []
    return android(requests, world), listing, requests


def taps(requests: Requests) -> list[Any]:
    return [s["args"][0] for s in scripts(requests) if s["script"] == "mobile: clickGesture"]  # type: ignore[index]


@pytest.fixture(autouse=False)
def quick_scrolling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("witx_gateway.devices.appium.REST_POLL", 0)


async def test_an_option_far_down_a_list_is_scrolled_to_and_tapped(quick_scrolling: None) -> None:
    names = [f"Country {i}" for i in range(100)] + ["China +86"] + [f"Land {i}" for i in range(20)]
    device, listing, requests = countries(names)
    result = await device.select_option(300, 400, "China +86")
    assert result.selected == "China +86"
    assert listing.offset > 0
    (tap,) = taps(requests)
    row = names.index("China +86") - listing.offset
    assert tap == {"x": 300.0, "y": 125.0 + 60 * row}  # the centre of the row it found


async def test_an_option_already_on_the_screen_is_tapped_without_scrolling(
    quick_scrolling: None,
) -> None:
    device, listing, requests = countries([f"Country {i}" for i in range(50)])
    result = await device.select_option(300, 400, "Country 3")
    assert result.selected == "Country 3" and listing.offset == 0
    assert not [a for r in requests if r[1].endswith("/actions") for a in [r]]  # never swiped


async def test_an_option_above_the_start_position_is_found_by_going_back_up(
    quick_scrolling: None,
) -> None:
    device, listing, _ = countries([f"Country {i}" for i in range(60)])
    listing.offset = 40  # the list is already scrolled to the bottom
    result = await device.select_option(300, 400, "Country 2")
    assert result.selected == "Country 2"


async def test_scrolling_for_an_option_holds_still_before_letting_go_so_the_list_stops(
    quick_scrolling: None,
) -> None:
    device, _, requests = countries([f"Country {i}" for i in range(30)] + ["China +86"])
    await device.select_option(300, 400, "China +86")
    scrolls = gestures(requests)
    assert scrolls and all(steps[-2:] == [wait(150), UP] for steps in scrolls)


async def test_an_option_that_is_not_in_the_list_taps_nothing(quick_scrolling: None) -> None:
    device, _, requests = countries([f"Country {i}" for i in range(30)])
    result = await device.select_option(300, 400, "Atlantis")
    assert (result.selected, result.problem) == (None, "not_found")
    assert taps(requests) == []  # scrolled through to the end and back, and tapped nothing


async def test_two_options_showing_the_same_text_are_not_guessed_between(
    quick_scrolling: None,
) -> None:
    device, _, requests = countries(["Home", "Work", "Other", "Home", "X", "Y", "Z", "W", "V"])
    result = await device.select_option(300, 400, "Home")
    assert (result.selected, result.problem) == (None, "ambiguous")
    assert taps(requests) == []


async def test_a_list_that_cannot_be_read_before_the_first_swipe_says_why_and_touches_nothing(
    quick_scrolling: None,
) -> None:
    device, _, requests = countries([f"Country {i}" for i in range(30)], {"locked": True})
    result = await device.select_option(300, 400, "Country 20")
    assert (result.selected, result.problem) == (None, "locked")
    assert gestures(requests) == [] and taps(requests) == []


async def test_the_search_for_an_option_gives_up_when_its_time_is_up(
    quick_scrolling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("witx_gateway.devices.appium.SELECT_BUDGET", 0)
    device, _, requests = countries([f"Country {i}" for i in range(500)])
    result = await device.select_option(300, 400, "Country 400")
    assert result.problem == "not_found" and taps(requests) == []


async def test_two_calls_at_once_open_one_session_not_two() -> None:
    requests: Requests = []
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests),
    )
    await asyncio.gather(device.warm(), device.state(), device.state())
    opened = [r for r in requests if r[0] == "POST" and r[1] == "/session"]
    assert len(opened) == 1


async def test_warming_opens_the_session_reads_the_geometry_then_only_checks_it_is_on() -> None:
    requests: Requests = []
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests),
        keep_warm=True,
    )
    assert device.last_state is None
    await device.warm()
    assert requests[0][1] == "/session" and device.last_state is not None
    opened = len(requests)
    await device.warm()
    assert [(r[1], (r[2] or {}).get("script")) for r in requests[opened:]] == [  # type: ignore[union-attr]
        ("/session/S1/execute/sync", "mobile: isLocked"),
        ("/session/S1/rotation", None),  # cheap: no element tree, and a dark screen shows
    ]


async def test_a_dark_screen_counts_as_locked_and_is_found_before_reading_the_front_app() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"rotation": -1}  # WebDriverAgent: the front app says nothing
    device = ios_device("UDID-1", world, requests)
    with pytest.raises(DeviceError, match="locked"):
        await device.state()
    assert device.locked
    assert "mobile: activeAppInfo" not in settings_and_scripts(requests)  # seconds, for nothing
    world["rotation"] = 0  # someone woke it
    await device.state()
    assert not device.locked


def session_capabilities(requests: Requests) -> list[dict[str, Any]]:
    return [
        b["capabilities"]["alwaysMatch"]  # type: ignore[index]
        for m, path, b in requests
        if m == "POST" and path == "/session"
    ]


def unauthorized_iphone(
    requests: Requests, monkeypatch: pytest.MonkeyPatch, *, passcode: bool | None
) -> tuple[IOSDevice, dict[str, Any]]:
    """A real iPhone whose agent lost its rights while it was locked: it sees nothing of
    the screen and its screenshots fail with XCTest's code 41. ``passcode``: whether the
    phone itself says it still needs its passcode (None: it cannot be asked). Its agent's
    process, when stopped, is counted in ``world["stopped"]``."""

    async def asked(udid: str) -> bool | None:
        return passcode

    async def stop(udid: str) -> bool:
        world["stopped"] += 1
        return True

    monkeypatch.setattr(ios_module, "passcode_required", asked)
    monkeypatch.setattr(ios_module, "stop_agent", stop)
    world: dict[str, Any] = {"rotation": -1, "unauthorized": True, "stopped": 0}
    return ios_device("UDID-1", world, requests), world


async def test_an_agent_that_lost_its_rights_on_an_unlocked_iphone_is_stopped_not_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: Requests = []
    device, world = unauthorized_iphone(requests, monkeypatch, passcode=False)
    with pytest.raises(DeviceError, match="session_lost"):
        await device.state()
    assert not device.locked  # it is not locked: its agent is broken
    assert ("DELETE", "/session/S1") in [(m, p) for m, p, _ in requests]
    assert world["stopped"] == 1  # its process, so that the next session starts it again
    world.update(rotation=0, unauthorized=False)  # the new agent sees the screen
    await device.state()
    # Uninstalling it would take the phone's trust in a free team's certificate with it.
    assert all("appium:useNewWDA" not in caps for caps in session_capabilities(requests))


@pytest.mark.parametrize("passcode", [True, None])
async def test_an_iphone_that_still_needs_its_passcode_stays_locked_and_keeps_its_agent(
    monkeypatch: pytest.MonkeyPatch, passcode: bool | None
) -> None:
    requests: Requests = []
    device, world = unauthorized_iphone(requests, monkeypatch, passcode=passcode)
    with pytest.raises(DeviceError, match="locked"):
        await device.state()
    assert device.locked
    assert ("DELETE", "/session/S1") not in [(m, p) for m, p, _ in requests]
    assert world["stopped"] == 0


async def test_an_agent_is_renewed_at_most_once_in_a_while(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: Requests = []
    device, world = unauthorized_iphone(requests, monkeypatch, passcode=False)
    with pytest.raises(DeviceError, match="session_lost"):
        await device.state()
    with pytest.raises(DeviceError, match="locked"):  # the new agent did not help
        await device.state()
    assert len(session_capabilities(requests)) == 2
    assert world["stopped"] == 1


async def test_stopping_the_agent_ends_only_its_process_and_uninstalls_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    agent = "file:///private/var/containers/Bundle/Application/X/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner"

    async def devicectl(udid: str, *command: str) -> dict[str, Any] | None:
        commands.append(command)
        if command[:3] == ("device", "info", "processes"):
            processes = [
                {"executable": "file:///sbin/launchd", "processIdentifier": 1},
                {"executable": agent, "processIdentifier": 812},
            ]
            return {"runningProcesses": processes}
        return {}

    monkeypatch.setattr(ios_module, "_devicectl", devicectl)
    assert await ios_module.stop_agent("UDID-1")
    assert commands[1:] == [("device", "process", "terminate", "--pid", "812", "--kill")]


async def test_a_dark_simulator_is_locked_without_asking_anything_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(udid: str) -> bool | None:
        raise AssertionError("a simulator has no passcode to ask about")

    monkeypatch.setattr(ios_module, "passcode_required", never)
    requests: Requests = []
    world: dict[str, Any] = {
        "rotation": -1,
        "unauthorized": True,
        "device_info": {"isSimulator": True},
    }
    device = IOSDevice(
        udid=SIMULATOR,
        name="iPhone 18 Pro",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    with pytest.raises(DeviceError, match="locked"):
        await device.state()
    assert not any(p.endswith("/screenshot") for _, p, _ in requests)


async def test_a_locked_phone_is_remembered_until_a_look_finds_it_on() -> None:
    world: dict[str, Any] = {"locked": True}
    device = android([], world)
    with pytest.raises(DeviceError, match="locked"):
        await device.state()
    assert device.locked
    with pytest.raises(DeviceError, match="locked"):
        await device.check_lock()
    world["locked"] = False
    await device.check_lock()
    assert not device.locked


async def test_waking_a_phone_asks_its_driver_to_unlock_it() -> None:
    requests: Requests = []
    await ios_device("UDID-1", {}, requests).wake()
    assert scripts(requests)[-1] == {"script": "mobile: unlock", "args": []}


async def test_a_session_that_was_lost_is_dropped_by_warming_and_opened_again_next_time() -> None:
    requests: Requests = []
    world: dict[str, Any] = {"sessions": ["GONE", "S2"]}
    device = AndroidDevice(
        serial="emulator-5554",
        name="Pixel",
        appium_url="http://127.0.0.1:4723",
        transport=appium(requests, world),
    )
    with pytest.raises(DeviceError, match="session_lost"):
        await device.warm()  # opens "GONE", which the driver has since lost
    await device.warm()  # a new one: S2
    assert [r[1] for r in requests].count("/session") == 2
    assert device.last_state is not None


async def test_an_iphone_presses_its_volume_keys_but_a_simulator_has_none() -> None:
    requests: Requests = []
    phone = ios_device("00000000-0000000000000001", {}, requests)
    await phone.volume_up()
    await phone.volume_down()
    await phone.lock()
    assert scripts(requests) == [
        {"script": "mobile: pressButton", "args": [{"name": "volumeUp"}]},
        {"script": "mobile: pressButton", "args": [{"name": "volumeDown"}]},
        {"script": "mobile: lock", "args": []},
    ]
    assert {"volume_up", "volume_down", "lock"} <= set(phone.capabilities)
    simulator = ios_device(SIMULATOR, {})
    assert "lock" in simulator.capabilities
    assert not {"volume_up", "volume_down"} & set(simulator.capabilities)


def test_a_pinch_spreads_its_fingers_over_most_of_the_screen_and_keeps_them_on_it() -> None:
    # Zoom out: from 0.7 of the short side apart to a quarter of that, around the point.
    assert pinch_fingers(201, 0.25, 402, 874) == (201, 140.7, 35.175)
    # Zoom in: the other way round.
    assert pinch_fingers(201, 4, 402, 874) == (201, 35.175, 140.7)
    # Near an edge the middle moves in, so that neither finger leaves the screen.
    middle, start, _ = pinch_fingers(20, 0.5, 402, 874)
    assert middle - start >= 0 and middle == pytest.approx(160.8)
