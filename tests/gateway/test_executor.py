"""Gateway execution: what reaches the device, and what never does."""

import asyncio
import base64
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from PIL import Image
from witx_contracts import gateway as wire
from witx_gateway import executor
from witx_gateway.devices.base import DeviceError, TextField, fit_png, png_size
from witx_gateway.devices.fake import FakeDevice
from witx_gateway.executor import Executor
from witx_gateway.journal import Journal
from witx_gateway.mjpeg import StreamFrame
from witx_gateway.sliders import SliderReading

from tests.gateway.test_devices import Requests, android

DEVICE_ID = uuid4()


class Harness:
    def __init__(self, tmp_path: Path, device: FakeDevice | None = None) -> None:
        self.device = device or FakeDevice()
        self.journal = Journal(tmp_path / "journal.sqlite3")
        self.sent: list[dict] = []  # type: ignore[type-arg]
        self.uploads: dict[UUID, bytes] = {}
        self.executor = Executor(self.journal, [self.device], self._send, self._upload)
        self.executor.bind(
            [wire.DeviceBinding(native_id=self.device.native_id, device_id=DEVICE_ID)]
        )
        self.journal.set_epoch(DEVICE_ID, 7)

    async def _send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def _upload(self, observation_id: UUID, data: bytes) -> None:
        self.uploads[observation_id] = data

    def screen(self, **overrides: object) -> wire.ScreenState:
        d = self.device
        values = {
            "width": d.width * d.scale,
            "height": d.height * d.scale,
            "input_width": d.width,
            "input_height": d.height,
            "rotation": 0,
            "foreground_app": d.foreground_app,
        }
        values.update(overrides)
        return wire.ScreenState(**values)  # type: ignore[arg-type]

    def action(self, **overrides: object) -> wire.Action:
        values: dict[str, object] = {
            "action_id": uuid4(),
            "device_id": DEVICE_ID,
            "epoch": 7,
            "input": {"kind": "tap", "x": 300, "y": 600},
            "expected": self.screen(),
            "digest": "a" * 64,
            "expires_at": datetime.now(UTC) + timedelta(seconds=30),
            "post_observation_id": uuid4(),
        }
        values.update(overrides)
        return wire.Action.model_validate(values)

    def results(self) -> list[dict]:  # type: ignore[type-arg]
        return [m for m in self.sent if m["type"] == "action_result"]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_an_admitted_action_runs_once_and_is_followed_by_a_screenshot(h: Harness) -> None:
    action = h.action()
    await h.executor.action(action)
    await h.executor.action(action)  # redelivered
    assert h.device.inputs == [("tap", (300.0, 600.0))]
    results = h.results()
    assert [r["outcome"] for r in results] == ["returned", "returned"]  # result repeated, input not
    post = [m for m in h.sent if m["type"] == "observation_result"]
    assert post[0]["observation_id"] == str(action.post_observation_id)
    assert action.post_observation_id in h.uploads


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"epoch": 6}, "stale_epoch"),
        ({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, "expired"),
        ({"input": {"kind": "tap", "x": 5000, "y": 10}}, "out_of_bounds"),
    ],
)
async def test_checks_before_input(h: Harness, overrides: dict, reason: str) -> None:  # type: ignore[type-arg]
    await h.executor.action(h.action(**overrides))
    assert h.device.inputs == []
    assert h.results()[0] == h.results()[0] | {"outcome": "rejected", "reason": reason}
    # No follow-up screenshot when nothing happened.
    assert not [m for m in h.sent if m["type"] == "observation_result"]


async def test_any_app_on_the_device_may_be_opened(h: Harness) -> None:
    await h.executor.action(h.action(input={"kind": "launch_app", "app_id": "com.tencent.mm"}))
    assert h.device.inputs == [("launch_app", "com.tencent.mm")]


async def test_an_app_that_is_not_installed_is_refused_before_anything_happens(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(installed_apps=["com.huawei.calculator"]))
    await h.executor.action(
        h.action(input={"kind": "launch_app", "app_id": "com.android.calculator2"})
    )
    assert h.device.inputs == []
    assert h.results()[0] == h.results()[0] | {"outcome": "rejected", "reason": "app_not_installed"}


async def test_an_app_search_answers_with_matches_and_touches_nothing(tmp_path: Path) -> None:
    device = FakeDevice(installed_apps=["com.huawei.calculator", "com.android.settings"])
    h = Harness(tmp_path, device)
    await h.executor.action(h.action(input={"kind": "find_app", "query": "calculator"}))
    assert h.device.inputs == []
    assert h.results()[0]["outcome"] == "returned"
    assert h.results()[0]["apps"] == [{"app_id": "com.huawei.calculator", "name": None}]


async def test_a_screenshot_that_cannot_be_uploaded_is_reported_failed(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(observation_id: UUID, data: bytes) -> None:
        raise OSError("cloud unreachable")

    monkeypatch.setattr(h.executor, "upload", failing)
    action = h.action()
    await h.executor.action(action)
    assert h.results()[0]["outcome"] == "returned"  # the input itself happened, once
    (post,) = [m for m in h.sent if m["type"] == "observation_result"]
    assert post["error"] == "upload_failed"


async def test_a_changed_screen_is_not_touched(h: Harness) -> None:
    action = h.action(expected=h.screen(foreground_app="com.other"))
    await h.executor.action(action)
    assert h.device.inputs == []
    assert h.results()[0]["reason"] == "stale_observation"


async def test_a_phone_turned_the_other_way_round_is_not_touched(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(width=2400, height=1080, rotation=90))
    action = h.action(expected=h.screen(rotation=90))
    h.device.rotation = 270  # same size, upside down: the tap would land mirrored
    await h.executor.action(action)
    assert h.device.inputs == []
    assert h.results()[0]["reason"] == "stale_observation"


async def test_screenshot_pixels_are_scaled_to_input_points(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(width=402, height=874, scale=3))
    await h.executor.action(h.action(input={"kind": "tap", "x": 603, "y": 1311}))
    assert h.device.inputs == [("tap", (201.0, 437.0))]


async def test_large_screenshots_are_shrunk_to_what_the_model_sees(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(width=402, height=874, scale=3))  # 1206 x 2622 pixels
    observation_id = uuid4()
    await h.executor.observe(wire.Observe(observation_id=observation_id, device_id=DEVICE_ID))
    screen = wire.ScreenState.model_validate(h.sent[0]["screen"])
    assert (screen.width, screen.height) == (768, 1670)
    assert png_size(h.uploads[observation_id]) == (768, 1670)
    # A point picked on the shrunk screenshot lands on the same spot of the device.
    await h.executor.action(h.action(input={"kind": "tap", "x": 384, "y": 835}, expected=screen))
    assert h.device.inputs == [("tap", (201.0, 437.0))]


@pytest.mark.parametrize(
    ("given", "done"),
    [
        (
            {"kind": "swipe", "x1": 300, "y1": 2400, "x2": 600, "y2": 1800, "duration_ms": 200},
            ("swipe", (100.0, 800.0, 200.0, 600.0, 200, 0, None)),
        ),
        (
            {"kind": "swipe", "x1": 300, "y1": 2400, "x2": 300, "y2": 2100, "duration_ms": 150}
            | {"hold_ms": 4000},
            ("swipe", (100.0, 800.0, 100.0, 700.0, 150, 4000, None)),
        ),
        (
            {"kind": "long_press", "x": 603, "y": 1311, "duration_ms": 800},
            ("long_press", (201.0, 437.0, 800)),
        ),
        ({"kind": "double_tap", "x": 603, "y": 1311}, ("double_tap", (201.0, 437.0))),
        (
            {"kind": "pinch", "x": 603, "y": 1311, "scale": 0.25},
            ("pinch", (201.0, 437.0, 140.7, 35.175)),  # 0.7 of 402 apart, then a quarter
        ),
    ],
)
async def test_gestures_reach_the_device_in_input_points_with_their_timing(
    tmp_path: Path,
    given: dict,  # type: ignore[type-arg]
    done: tuple[str, object],
) -> None:
    h = Harness(tmp_path, FakeDevice(width=402, height=874, scale=3))
    await h.executor.action(h.action(input=given))
    assert h.device.inputs == [done]
    assert h.results()[0]["outcome"] == "returned"


@pytest.mark.parametrize(
    "given",
    [
        {"kind": "long_press", "x": 5000, "y": 10, "duration_ms": 800},
        {"kind": "double_tap", "x": 10, "y": 5000},
        {"kind": "pinch", "x": 10, "y": 5000, "scale": 2},
    ],
)
async def test_a_press_outside_the_screenshot_is_refused(h: Harness, given: dict) -> None:  # type: ignore[type-arg]
    await h.executor.action(h.action(input=given))
    assert h.device.inputs == []
    assert h.results()[0] == h.results()[0] | {"outcome": "rejected", "reason": "out_of_bounds"}


async def test_a_device_without_the_gesture_is_not_asked_to_make_it(tmp_path: Path) -> None:
    device = FakeDevice()
    device.capabilities = ["tap", "swipe"]
    h = Harness(tmp_path, device)
    await h.executor.action(h.action(input={"kind": "double_tap", "x": 300, "y": 600}))
    assert h.device.inputs == []
    assert h.results()[0]["reason"] == "unsupported_input"


async def test_a_picker_input_turns_the_wheel_under_the_point(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(width=402, height=874, scale=3))
    await h.executor.action(
        h.action(input={"kind": "set_picker", "x": 603, "y": 1311, "value": "10"})
    )
    assert h.device.inputs == [("set_picker", (201.0, 437.0, "10"))]
    assert h.results()[0]["outcome"] == "returned"


async def test_a_picker_input_with_no_wheel_under_the_point_is_rejected(h: Harness) -> None:
    h.device.picker = False
    action = h.action(input={"kind": "set_picker", "x": 300, "y": 600, "value": "10"})
    await h.executor.action(action)
    assert h.device.inputs == []
    assert h.results()[0] == h.results()[0] | {"outcome": "rejected", "reason": "no_picker"}


async def test_a_failed_input_call_is_unknown_not_retried(h: Harness) -> None:
    h.device.fail_next = "connection_lost"
    action = h.action()
    await h.executor.action(action)
    await h.executor.action(action)
    assert h.results()[0]["outcome"] == "unknown"
    assert h.device.inputs == []


async def test_after_a_crash_an_intended_action_is_reported_unknown(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    action = h.action()
    h.journal.receive(action.action_id, DEVICE_ID, action.digest)
    h.journal.intent(action.action_id)  # crash happens here
    h.journal.close()

    restarted = Harness(tmp_path)
    restarted.executor.recover()
    (queued,) = [json.loads(m) for m in restarted.journal.outbox()]
    assert queued["outcome"] == "unknown"
    await restarted.executor.action(action)  # the cloud resends it
    assert restarted.device.inputs == []


async def test_stop_raises_a_barrier_and_waits_for_input_in_flight(h: Harness) -> None:
    release = asyncio.Event()
    original_tap = h.device.tap

    async def slow_tap(x: float, y: float) -> None:
        await release.wait()
        await original_tap(x, y)

    h.device.tap = slow_tap  # type: ignore[method-assign]
    in_flight = asyncio.create_task(h.executor.action(h.action()))
    await asyncio.sleep(0.05)
    stop = wire.Stop(stop_id=uuid4(), device_id=DEVICE_ID, epoch=7)
    h.executor.barrier(stop)
    confirming = asyncio.create_task(h.executor.confirm_stop(stop))
    await asyncio.sleep(0.05)
    assert not [m for m in h.sent if m["type"] == "stop_ack"]  # not before the input finishes
    release.set()
    await in_flight
    await confirming
    assert [m["type"] for m in h.sent][-1] == "stop_ack"

    await h.executor.action(h.action())  # same epoch after the barrier
    assert h.results()[-1]["reason"] == "stopped"


async def test_epochs_never_go_back(h: Harness) -> None:
    h.journal.set_epoch(DEVICE_ID, 9)
    h.journal.set_epoch(DEVICE_ID, 8)
    assert h.journal.epochs(DEVICE_ID) == (9, 0)


async def test_observation_is_answered_once(h: Harness) -> None:
    obs = wire.Observe(observation_id=uuid4(), device_id=DEVICE_ID)
    await h.executor.observe(obs)
    await h.executor.observe(obs)
    results = [m for m in h.sent if m["type"] == "observation_result"]
    assert len(results) == 2 and results[0] == results[1]
    assert results[0]["screen"]["foreground_app"] == "com.example.app"


async def test_a_screenshot_lists_the_sliders_in_view_where_they_are_on_the_picture(
    tmp_path: Path,
) -> None:
    device = FakeDevice()
    device.sliders = [SliderReading("Font size", "1.0", (100.0, 200.0, 400.0, 100.0))]
    h = Harness(tmp_path, device)
    await h.executor.observe(wire.Observe(observation_id=uuid4(), device_id=DEVICE_ID))
    screen = next(m for m in h.sent if m["type"] == "observation_result")["screen"]
    scale = screen["width"] / screen["input_width"]  # the screenshot is shrunk to fit
    (slider,) = screen["sliders"]
    assert (slider["name"], slider["value"]) == ("Font size", "1.0")
    assert (slider["x"], slider["y"]) == pytest.approx((300 * scale, 250 * scale), rel=1e-3)


async def test_a_screenshot_is_taken_even_when_the_sliders_cannot_be_read(h: Harness) -> None:
    async def broken() -> list[SliderReading]:
        raise DeviceError("appium_error")

    h.device.read_sliders = broken  # type: ignore[method-assign]
    await h.executor.observe(wire.Observe(observation_id=uuid4(), device_id=DEVICE_ID))
    result = next(m for m in h.sent if m["type"] == "observation_result")
    assert result["screen"]["sliders"] == [] and result["error"] is None


async def test_a_screen_that_changes_while_it_settles_is_captured_once_settled(
    tmp_path: Path,
) -> None:
    # iOS after a home press: the state read before the first shot still names the app,
    # the settled screen is the home screen.
    h = Harness(tmp_path, FakeDevice(settles_on_its_own=False))
    original = h.device.screenshot
    switched = False

    async def switching() -> bytes:
        nonlocal switched
        data = await original()
        if not switched:
            switched = True
            h.device.foreground_app = "com.apple.springboard"
        return data

    h.device.screenshot = switching  # type: ignore[method-assign]
    action = h.action(input={"kind": "home"})
    await h.executor.action(action)
    post = next(m for m in h.sent if m["type"] == "observation_result")
    assert post["error"] is None
    assert post["screen"]["foreground_app"] == "com.apple.springboard"


async def test_capture_fails_when_the_screen_changes_during_capture(h: Harness) -> None:
    original = h.device.screenshot

    async def switching() -> bytes:
        data = await original()
        h.device.foreground_app = "com.other"
        return data

    h.device.screenshot = switching  # type: ignore[method-assign]
    await h.executor.observe(wire.Observe(observation_id=uuid4(), device_id=DEVICE_ID))
    assert h.sent[-1]["error"] == "screen_changed"


def test_device_error_has_a_reason() -> None:
    assert DeviceError("locked").reason == "locked"


async def test_text_is_never_typed_into_a_password_field(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(secure_field=True))
    await h.executor.action(h.action(input={"kind": "type_text", "text": "hunter2"}))
    assert h.device.inputs == []
    assert h.results()[0] == h.results()[0] | {"outcome": "rejected", "reason": "secure_field"}


async def test_a_redelivery_with_other_parameters_is_not_executed(h: Harness) -> None:
    action = h.action()
    await h.executor.action(action)
    await h.executor.action(action.model_copy(update={"digest": "b" * 64}))
    assert h.device.inputs == [("tap", (300.0, 600.0))]
    assert len(h.results()) == 1


# ---------------------------------------------------------------- takeover


def manual(**overrides: object) -> wire.ManualInput:
    values: dict[str, object] = {
        "input_id": uuid4(),
        "device_id": DEVICE_ID,
        "epoch": 7,
        "input": {"kind": "tap", "x": 300, "y": 600},
        "screen": {
            "width": 1080,
            "height": 2400,
            "input_width": 1080,
            "input_height": 2400,
            "rotation": 0,
            "foreground_app": "com.example.app",
        },
    }
    values.update(overrides)
    return wire.ManualInput.model_validate(values)


def manual_results(h: Harness) -> list[dict]:  # type: ignore[type-arg]
    return [m for m in h.sent if m["type"] == "manual_input_result"]


async def test_a_persons_input_runs_once_under_the_takeover_epoch(h: Harness) -> None:
    message = manual()
    await h.executor.manual(message)
    await h.executor.manual(message)  # redelivered
    assert h.device.inputs == [("tap", (300.0, 600.0))]
    assert [r["outcome"] for r in manual_results(h)] == ["returned"]


async def test_a_persons_input_outside_the_takeover_epoch_is_refused(h: Harness) -> None:
    await h.executor.manual(manual(epoch=6))
    h.journal.stop(DEVICE_ID, 7)  # the takeover ended
    await h.executor.manual(manual(epoch=7))
    assert h.device.inputs == []
    assert [r["reason"] for r in manual_results(h)] == ["stale_epoch", "stopped"]


async def test_a_persons_tap_on_a_frame_from_before_the_phone_turned_is_refused(
    h: Harness,
) -> None:
    h.device.rotation = 180
    await h.executor.manual(manual())
    assert h.device.inputs == []
    assert manual_results(h)[0]["reason"] == "screen_changed"


async def test_a_person_may_hold_a_point_but_not_double_tap(h: Harness) -> None:
    await h.executor.manual(
        manual(input={"kind": "long_press", "x": 300, "y": 600, "duration_ms": 900})
    )
    await h.executor.manual(manual(input={"kind": "double_tap", "x": 300, "y": 600}))
    assert h.device.inputs == [("long_press", (300.0, 600.0, 900))]
    assert [r["outcome"] for r in manual_results(h)] == ["returned", "rejected"]


def dragged(**overrides: object) -> dict[str, object]:
    """A person's drag with its path: a bent way from (100, 200) to (300, 500)."""
    return {
        "kind": "swipe",
        "x1": 100,
        "y1": 200,
        "x2": 300,
        "y2": 500,
        "duration_ms": 400,
        "path": [
            {"x": 100, "y": 200, "t_ms": 0},
            {"x": 250, "y": 200, "t_ms": 160},
            {"x": 300, "y": 500, "t_ms": 400},
        ],
    } | overrides


async def test_a_persons_drag_reaches_the_device_along_its_path_in_input_points(
    h: Harness,
) -> None:
    half = manual().screen.model_copy(update={"width": 540, "height": 1200})  # a shrunk frame
    await h.executor.manual(manual(input=dragged(), screen=half))
    path = [(200.0, 400.0, 0), (500.0, 400.0, 160), (600.0, 1000.0, 400)]
    assert h.device.inputs == [("swipe", (200.0, 400.0, 600.0, 1000.0, 400, 0, path))]
    assert manual_results(h)[0]["outcome"] == "returned"


async def test_a_drag_whose_path_leaves_the_frame_is_refused(h: Harness) -> None:
    off = dict(dragged())
    off["path"] = [
        {"x": 100, "y": 200, "t_ms": 0},
        {"x": 1080, "y": 200, "t_ms": 160},  # the frame is 1080 wide: one pixel past it
        {"x": 300, "y": 500, "t_ms": 400},
    ]
    await h.executor.manual(manual(input=off))
    assert h.device.inputs == []
    assert manual_results(h)[0]["reason"] == "out_of_bounds"


async def test_a_person_may_type_into_a_secure_field(h: Harness) -> None:
    h.device.secure_field = True  # the agent is refused here; the person is not
    await h.executor.manual(manual(input={"kind": "type_text", "text": "hunter2"}))
    assert h.device.inputs == [("type_text", "hunter2")]
    assert manual_results(h)[0]["outcome"] == "returned"


async def test_a_frame_is_a_jpeg_of_the_shrunk_screen(h: Harness) -> None:
    await h.executor.frame(wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID))
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert base64.b64decode(frame["jpeg"])[:2] == b"\xff\xd8"  # JPEG
    assert (frame["screen"]["width"], frame["screen"]["height"]) == (768, 1707)


async def test_a_frame_can_be_scaled_down_without_changing_the_screen_it_reports(
    h: Harness,
) -> None:
    await h.executor.frame(wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID, max_side=400))
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    with Image.open(io.BytesIO(base64.b64decode(frame["jpeg"]))) as picture:
        assert max(picture.size) == 400
    assert (frame["screen"]["width"], frame["screen"]["height"]) == (768, 1707)


def broadcast_of(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (90, 90, 90)).save(out, format="JPEG")
    return out.getvalue()


def streamed(**overrides: object) -> wire.FrameRequest:
    values: dict[str, object] = {"request_id": uuid4(), "device_id": DEVICE_ID, "stream": True}
    values.update(overrides)
    return wire.FrameRequest.model_validate(values)


async def test_a_streamed_frame_is_the_broadcast_picture_with_its_own_size_as_the_screen(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    await h.executor.frame(streamed())
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert base64.b64decode(frame["jpeg"]) == h.device.broadcast  # not encoded again
    assert (frame["screen"]["width"], frame["screen"]["height"]) == (540, 1200)
    assert (frame["screen"]["input_width"], frame["screen"]["input_height"]) == (1080, 2400)


async def test_a_streamed_frame_larger_than_asked_for_is_shrunk_and_reports_the_shrunk_size(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(1080, 2400)))
    await h.executor.frame(streamed(max_side=1200))
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert (frame["screen"]["width"], frame["screen"]["height"]) == (540, 1200)


async def test_pictures_asked_for_together_are_answered_one_at_a_time_each_newer_than_the_last(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    active = most = 0
    asked: list[int] = []
    original = h.device.stream_frame

    async def slow(after: int, wait: float) -> StreamFrame | None:
        nonlocal active, most
        active += 1
        most = max(most, active)
        asked.append(after)
        await asyncio.sleep(0.02)
        active -= 1
        return await original(after, wait)

    h.device.stream_frame = slow  # type: ignore[method-assign]
    await asyncio.gather(*(h.executor.frame(streamed()) for _ in range(3)))
    assert most == 1  # the cloud keeps requests in flight; the broadcast is read in turn
    assert asked == [0, 1, 2]  # each asks for one newer than the last it sent


async def hanging_after_the_first_read(h: Harness) -> list[int]:
    """The phone's geometry reads for ever after the first (WebDriverAgent waiting for an app
    that is never idle); the list counts the reads started."""
    reads: list[int] = []
    original = h.device.state

    async def state() -> Any:
        reads.append(1)
        if len(reads) > 1:
            await asyncio.sleep(3600)
        return await original()

    h.device.state = state  # type: ignore[method-assign]
    return reads


async def test_pictures_keep_coming_while_reading_the_phones_geometry_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "STREAM_STATE_TTL", 0.0)  # every picture wants a fresh read
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    reads = await hanging_after_the_first_read(h)
    await h.executor.frame(streamed())  # the first picture reads the geometry once
    started = asyncio.get_running_loop().time()
    for _ in range(5):
        await h.executor.frame(streamed())
    assert asyncio.get_running_loop().time() - started < 1.0
    frames = [m for m in h.sent if m["type"] == "frame"]
    assert len(frames) == 6 and all(f.get("jpeg") and not f.get("error") for f in frames)
    await asyncio.sleep(0.05)
    assert len(reads) == 2  # one refresh under way, not one per picture
    for _ in range(3):
        await h.executor.frame(streamed())
    await asyncio.sleep(0.05)
    assert len(reads) == 2
    for task in h.executor._refreshing.values():
        task.cancel()


async def test_a_picture_that_needs_the_geometry_before_it_was_read_waits_for_it(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    await h.executor.frame(streamed())
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert frame["screen"]["input_width"] == 1080  # read before the picture was sent


async def test_the_first_picture_uses_the_geometry_read_while_the_phone_was_kept_ready(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    h.device.last_state = await h.device.state()  # type: ignore[attr-defined]

    async def never() -> Any:
        await asyncio.sleep(3600)

    h.device.state = never  # type: ignore[method-assign]
    await asyncio.wait_for(h.executor.frame(streamed()), 2)
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert frame.get("jpeg") and frame["screen"]["input_width"] == 1080


async def test_a_phone_turned_while_its_geometry_cannot_be_read_shows_no_wrong_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "STREAM_STATE_TIMEOUT", 0.05)
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    await hanging_after_the_first_read(h)
    assert await h.executor._broadcast(h.device, streamed()) is not None  # geometry known
    h.device.broadcast = broadcast_of(1200, 540)  # turned: the picture no longer fits it
    started = asyncio.get_running_loop().time()
    assert await h.executor._broadcast(h.device, streamed()) is None  # not a stretched picture
    assert asyncio.get_running_loop().time() - started < 1.0  # and not a stalled one


async def test_a_tap_on_a_streamed_frame_lands_where_the_person_pointed(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    await h.executor.frame(streamed())
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    screen = wire.ScreenState.model_validate(frame["screen"])
    await h.executor.manual(manual(input={"kind": "tap", "x": 270, "y": 600}, screen=screen))
    assert h.device.inputs == [("tap", (540.0, 1200.0))]


async def test_a_phone_without_a_broadcast_gives_a_screenshot_for_a_streamed_frame(
    h: Harness,
) -> None:
    await h.executor.frame(streamed())
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert (frame["screen"]["width"], frame["screen"]["height"]) == (768, 1707)


async def test_a_broadcast_picture_of_another_shape_than_the_screen_is_not_shown(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(1200, 540)))  # turned, phone not
    await h.executor.frame(streamed())
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert (frame["screen"]["width"], frame["screen"]["height"]) == (768, 1707)


async def test_a_person_sees_the_phone_while_an_input_is_under_way(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(broadcast=broadcast_of(540, 1200)))
    await h.executor.frame(streamed())  # the phone's geometry is known now
    async with h.executor._lock(DEVICE_ID):  # a swipe is under way
        async with asyncio.timeout(1):
            await h.executor.frame(streamed())
    assert [m["type"] for m in h.sent] == ["frame", "frame"]
    assert all(m["jpeg"] for m in h.sent)


async def test_a_watched_frame_is_skipped_while_the_device_carries_out_an_input(
    h: Harness,
) -> None:
    async with h.executor._lock(DEVICE_ID):  # an input or screenshot under way
        await h.executor.frame(
            wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID, if_idle=True)
        )
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert frame["error"] == "busy" and frame["jpeg"] is None


async def test_a_watched_picture_that_hangs_lets_go_of_the_phone_in_time_for_the_next_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wire, "LIVE_FRAME_TIMEOUT", 0.3)

    class Hanging(FakeDevice):
        hang = True

        async def screenshot(self) -> bytes:
            if self.hang:
                self.hang = False
                await asyncio.Event().wait()  # the driver never answers
            return await super().screenshot()

    h = Harness(tmp_path, Hanging())
    watched = asyncio.create_task(
        h.executor.frame(wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID, if_idle=True))
    )
    await asyncio.sleep(0.05)
    assert h.executor._lock(DEVICE_ID).locked()  # the picture holds the phone
    await asyncio.wait_for(h.executor.action(h.action()), 1.5)
    await watched
    assert h.results()[0]["outcome"] == "returned"
    assert [m["error"] for m in h.sent if m["type"] == "frame"] == ["timeout"]


async def test_a_watched_picture_never_opens_a_driver_session(tmp_path: Path) -> None:
    requests: Requests = []
    device = android(requests)  # no session yet: it opens with the next command
    h = Harness(tmp_path)
    h.executor = Executor(h.journal, [device], h._send, h._upload)
    h.executor.bind([wire.DeviceBinding(native_id=device.native_id, device_id=DEVICE_ID)])
    await h.executor.frame(wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID, if_idle=True))
    (frame,) = [m for m in h.sent if m["type"] == "frame"]
    assert frame["error"] == "session_lost" and requests == []
    # A person who took the phone over waits for the session to open.
    await h.executor.frame(wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID))
    assert ("POST", "/session") in [(m, p) for m, p, _ in requests]


async def test_typing_reports_what_the_field_holds_afterwards(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        FakeDevice(field="+86 ", drop_char=7, field_name="Phone", field_box=(10, 20, 300, 40)),
    )  # loses one digit
    await h.executor.action(h.action(input={"kind": "type_text", "text": "13700130810"}))
    assert h.results()[0]["outcome"] == "returned"
    assert h.results()[0]["field_text"] == "+86 1370013810"
    assert h.results()[0]["field_name"] == "Phone"  # and which field that was
    assert h.results()[0]["field_box"] == [10, 20, 300, 40]


async def test_a_field_that_cannot_be_read_back_leaves_the_input_returned(
    tmp_path: Path,
) -> None:
    class Unreadable(FakeDevice):
        async def read_text_field(self, typed: str = "") -> TextField:
            raise DeviceError("no_focused_field")

    h = Harness(tmp_path, Unreadable())
    await h.executor.action(h.action(input={"kind": "type_text", "text": "hello"}))
    assert h.results()[0]["outcome"] == "returned"
    assert h.results()[0]["field_text"] is None


async def test_a_password_field_is_never_read_back(tmp_path: Path) -> None:
    device = FakeDevice(field="secret")
    h = Harness(tmp_path, device)
    real_type = device.type_text

    async def type_then_focus_password(text: str) -> None:
        await real_type(text)
        device.secure_field = True  # e.g. the app moved focus to the password field

    device.type_text = type_then_focus_password  # type: ignore[method-assign]
    await h.executor.action(h.action(input={"kind": "type_text", "text": "ann"}))
    assert h.results()[0]["field_text"] is None


def swipe_down(h: Harness) -> wire.Action:
    return h.action(
        input={"kind": "swipe", "x1": 200, "y1": 900, "x2": 200, "y2": 300, "duration_ms": 460}
    )


async def test_the_screenshot_after_a_swipe_shows_where_the_list_came_to_rest(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(fling_shots=3))
    action = swipe_down(h)
    await h.executor.action(action)
    resting = fit_png(await h.device.screenshot())
    assert h.uploads[action.post_observation_id] == resting  # not a shot of the list gliding


async def test_a_screen_that_never_comes_to_rest_does_not_hold_the_next_step_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("witx_gateway.executor.SETTLE_MAX", 0.3)
    h = Harness(tmp_path, FakeDevice(fling_shots=10**6))  # a video, a carousel
    action = swipe_down(h)
    await asyncio.wait_for(h.executor.action(action), timeout=3)
    assert action.post_observation_id in h.uploads  # the last shot taken is still delivered


async def test_the_screenshot_after_a_pinch_waits_for_the_map_to_stop_zooming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("witx_gateway.executor.SETTLE_MAX", 0.05)  # a list rests sooner
    h = Harness(tmp_path, FakeDevice(fling_shots=5))
    action = h.action(input={"kind": "pinch", "x": 200, "y": 600, "scale": 0.5})
    await h.executor.action(action)
    resting = fit_png(await h.device.screenshot())
    assert h.uploads[action.post_observation_id] == resting  # not the map halfway


async def test_only_a_swipe_waits_for_the_screen_to_rest(tmp_path: Path) -> None:
    class Counting(FakeDevice):
        shots = 0

        async def screenshot(self) -> bytes:
            Counting.shots += 1
            return await super().screenshot()

    h = Harness(tmp_path, Counting())
    await h.executor.action(h.action())  # a tap
    assert Counting.shots == 1
    await h.executor.action(swipe_down(h))
    assert Counting.shots == 1 + 2  # a swipe takes shots until two in a row agree


async def test_every_input_waits_for_the_screen_to_rest_when_the_driver_does_not(
    tmp_path: Path,
) -> None:
    class Counting(FakeDevice):
        shots = 0

        async def screenshot(self) -> bytes:
            Counting.shots += 1
            return await super().screenshot()

    h = Harness(tmp_path, Counting(settles_on_its_own=False))  # iOS: the driver answers at once
    await h.executor.action(h.action())  # a tap: its transition may still be playing
    assert Counting.shots == 2  # shots until two in a row agree


async def test_a_platform_whose_lists_do_not_coast_is_not_made_to_wait(tmp_path: Path) -> None:
    class Counting(FakeDevice):
        shots = 0

        async def screenshot(self) -> bytes:
            Counting.shots += 1
            return await super().screenshot()

    Counting.shots = 0
    h = Harness(tmp_path, Counting(swipe_coasts=False))
    await h.executor.action(swipe_down(h))
    assert Counting.shots == 1  # iOS: the shot taken at once already shows the list at rest


def type_into(h: Harness, x: float | None, y: float | None, text: str = "Ann") -> wire.Action:
    return h.action(input={"kind": "type_text", "text": text, "field_x": x, "field_y": y})


async def test_text_goes_into_the_field_the_agent_pointed_at(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(field_name="First name", field_box=(100, 200, 300, 40)))
    await h.executor.action(type_into(h, 250, 220))
    assert h.device.inputs == [("type_text", "Ann")]
    assert h.results()[0]["outcome"] == "returned"
    assert h.results()[0]["field_name"] == "First name"


async def test_text_is_not_typed_when_the_focus_is_in_another_field(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(field_name="Search", field_box=(100, 200, 300, 40)))
    await h.executor.action(type_into(h, 250, 500))  # the agent pointed at a field further down
    assert h.device.inputs == []  # never typed
    result = h.results()[0]
    assert (result["outcome"], result["reason"]) == ("rejected", "wrong_field")
    assert (result["field_name"], result["field_box"]) == ("Search", [100, 200, 300, 40])
    assert not [m for m in h.sent if m["type"] == "observation_result"]  # nothing happened


async def test_text_that_replaces_the_field_leaves_only_itself(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(field="Ann Lee", field_box=(100, 200, 300, 40)))
    await h.executor.action(h.action(input={"kind": "type_text", "text": "Bo", "replace": True}))
    assert h.results()[0]["field_text"] == "Bo"


async def test_a_replacement_refused_for_a_secure_field_empties_nothing(tmp_path: Path) -> None:
    device = FakeDevice(field="hunter2", secure_field=True)
    h = Harness(tmp_path, device)
    await h.executor.action(h.action(input={"kind": "type_text", "text": "x", "replace": True}))
    assert h.results()[0]["reason"] == "secure_field"
    assert (h.device.inputs, device.field) == ([], "hunter2")


async def test_a_way_of_typing_the_phone_lacks_is_refused_before_any_input(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(type_methods=frozenset({"set", "paste"})))
    await h.executor.action(h.action(input={"kind": "type_text", "text": "你好", "method": "keys"}))
    result = h.results()[0]
    assert (result["outcome"], result["reason"]) == ("rejected", "unsupported_method")
    assert h.device.inputs == []


async def test_typing_that_stops_short_says_why_and_the_field_is_still_read_back(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(field="你好", typing_stops="no_candidate"))
    await h.executor.action(h.action(input={"kind": "type_text", "text": "世界", "method": "keys"}))
    result = h.results()[0]
    assert (result["outcome"], result["reason"]) == ("returned", "no_candidate")
    assert result["field_text"] == "你好"
    assert h.device.typed_with == ["keys"]


async def test_typing_without_a_field_point_is_not_checked(tmp_path: Path) -> None:
    # A person who took the phone over names no field; neither does an older caller.
    h = Harness(tmp_path, FakeDevice(field_box=(100, 200, 300, 40)))
    await h.executor.action(type_into(h, None, None))
    assert h.device.inputs == [("type_text", "Ann")]


async def test_the_field_box_is_reported_in_screenshot_pixels(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(scale=3, field_name="Title", field_box=(10, 20, 100, 20)))
    await h.executor.action(type_into(h, 60, 90))  # screenshot pixels: 3 per input point
    assert h.results()[0]["field_box"] == [30, 60, 300, 60]


def test_a_point_is_in_a_field_with_a_little_room_but_not_in_its_neighbour() -> None:
    from witx_gateway.devices.base import point_in_box

    box = (144.0, 1885.0, 992.0, 192.0)  # an Android field; the next one starts 240 lower
    assert point_in_box((600, 1980), box)
    assert point_in_box((600, 2100), box)  # a little below it, aimed by eye
    assert not point_in_box((600, 2220), box)  # the middle of the next field
    assert point_in_box((10, 10), None)  # an unreadable box refuses nothing


def select(h: Harness, value: str = "China +86", x: float = 300, y: float = 900) -> wire.Action:
    return h.action(input={"kind": "select_option", "x": x, "y": y, "value": value})


async def test_the_option_the_phone_tapped_is_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(options=["China +86"]))
    await h.executor.action(select(h))
    assert h.device.inputs == [("select_option", (300.0, 900.0, "China +86"))]
    result = h.results()[0]
    assert (result["outcome"], result["reason"], result["field_text"]) == (
        "returned",
        None,
        "China +86",
    )
    assert [m for m in h.sent if m["type"] == "observation_result"]  # a screenshot follows


async def test_an_option_that_is_not_found_is_returned_with_the_reason(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(options=["Japan +81"]))
    await h.executor.action(select(h))
    result = h.results()[0]
    assert (result["outcome"], result["reason"]) == ("returned", "not_found")
    assert result.get("field_text") is None  # the list was scrolled: the screen is checked


async def test_the_list_point_must_be_on_the_screenshot(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(options=["China +86"]))
    await h.executor.action(select(h, x=5000))
    assert h.device.inputs == []
    assert h.results()[0] == h.results()[0] | {"outcome": "rejected", "reason": "out_of_bounds"}


async def test_a_device_without_the_capability_is_not_asked_to_select(tmp_path: Path) -> None:
    device = FakeDevice(options=["China +86"])
    device.capabilities = ["tap", "swipe"]
    h = Harness(tmp_path, device)
    await h.executor.action(select(h))
    assert h.device.inputs == []
    assert h.results()[0]["reason"] == "unsupported_input"


def frames_sent(h: Harness) -> list[dict]:  # type: ignore[type-arg]
    return [m for m in h.sent if m["type"] == "frame"]


async def test_a_locked_phone_taken_over_is_woken_and_then_shows_its_screen(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(locked=True))
    await h.executor.frame(streamed())
    (frame,) = frames_sent(h)
    assert frame.get("jpeg") and not frame.get("error")
    assert h.device.wakes == 1


async def test_a_phone_with_a_passcode_stays_locked_and_is_not_woken_over_and_over(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice(locked=True, passcode=True))
    await h.executor.frame(streamed())
    await h.executor.frame(streamed())
    assert [f.get("error") for f in frames_sent(h)] == ["locked", "locked"]
    assert h.device.wakes == 1  # once in a while, not for every picture


async def test_a_person_presses_the_side_keys_of_a_phone_that_has_them(h: Harness) -> None:
    for kind in ("volume_up", "volume_down"):
        await h.executor.manual(manual(input={"kind": kind}))
    h.device.capabilities = ["tap"]
    await h.executor.manual(manual(input={"kind": "volume_up"}))
    assert h.device.inputs == [("volume_up", None), ("volume_down", None)]
    assert [r["reason"] for r in manual_results(h)] == [None, None, "unsupported_input"]


async def test_a_phone_the_person_locked_is_not_woken_and_its_next_picture_says_locked(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, FakeDevice())
    await h.executor.frame(streamed())
    await h.executor.manual(manual(input={"kind": "lock"}))
    await h.executor.frame(streamed())
    assert [f.get("error") for f in frames_sent(h)] == [None, "locked"]
    assert h.device.wakes == 0


async def test_watching_a_locked_phone_never_wakes_it(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeDevice(locked=True))
    await h.executor.frame(
        wire.FrameRequest(request_id=uuid4(), device_id=DEVICE_ID, max_side=640, if_idle=True)
    )
    assert [f.get("error") for f in frames_sent(h)] == ["locked"]
    assert h.device.wakes == 0
