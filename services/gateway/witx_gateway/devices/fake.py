"""An in-memory phone for tests and demos. Every input changes the screen colour."""

import asyncio
import dataclasses
from dataclasses import dataclass, field
from typing import Any

from witx_contracts.common import Capability, Platform, Rotation

from witx_gateway.devices.base import (
    DeviceError,
    DeviceState,
    Path,
    Selection,
    TextField,
    WrongField,
    point_in_box,
    solid_png,
)
from witx_gateway.mjpeg import StreamFrame
from witx_gateway.sliders import SliderReading


@dataclass
class FakeDevice:
    native_id: str = "fake-1"
    name: str = "Fake phone"
    sensitive_apps: list[str] = field(default_factory=list)
    width: int = 1080
    height: int = 2400
    scale: int = 1  # screenshot pixels per input point (iOS-like when > 1)
    rotation: Rotation = 0  # turning the phone round changes it; width and height may stay
    foreground_app: str = "com.example.app"
    platform: Platform = "fake"
    capabilities: list[Capability] = field(
        default_factory=lambda: [
            "tap",
            "swipe",
            "long_press",
            "double_tap",
            "pinch",
            "type_text",
            "set_picker",
            "select_option",
            "back",
            "home",
            "volume_up",
            "volume_down",
            "lock",
            "launch_app",
            "find_app",
        ]
    )
    inputs: list[tuple[str, Any]] = field(default_factory=list)
    fail_next: str | None = None  # make the next input raise (simulates a lost call)
    secure_field: bool = False  # the focused field is a password field
    installed_apps: list[str] | None = None  # None: every app is installed
    input_seconds: float = 0  # how long an input takes
    picker: bool = True  # a picker wheel lies under every point
    _moving: int = 0  # screenshots left that still show the list moving
    swipe_coasts: bool = True
    settles_on_its_own: bool = True
    options: list[str] = dataclasses.field(default_factory=list)  # what select_option finds
    sliders: list[SliderReading] = dataclasses.field(default_factory=list)
    fling_shots: int = 0  # screenshots after a swipe or pinch that still show it moving
    field: str = ""  # the focused text field's content
    field_name: str = ""  # what the app calls it
    field_box: tuple[float, float, float, float] | None = None  # where it is
    drop_char: int | None = None  # typing loses the character at this index
    broadcast: bytes | None = None  # a JPEG its screen broadcast shows; None: it has none
    session_open: bool = True  # False: like a phone whose driver session was lost
    locked: bool = False  # its screen is locked or dark
    passcode: bool = False  # waking it leaves it locked
    wakes: int = 0  # how often it was woken
    _pictures: int = 0

    async def state(self) -> DeviceState:
        if self.locked:
            raise DeviceError("locked")
        return DeviceState(self.width, self.height, self.rotation, self.foreground_app)

    async def wake(self) -> None:
        self.wakes += 1
        self.locked = self.passcode

    async def screenshot(self) -> bytes:
        grey = (40 * len(self.inputs)) % 200 + 30
        if self._moving > 0:  # the list is still gliding: every shot shows another place
            self._moving -= 1
            grey = (grey + 17 * (self._moving + 1)) % 200 + 30
        return solid_png(self.width * self.scale, self.height * self.scale, grey)

    async def stream_frame(self, after: int, wait: float) -> StreamFrame | None:
        if self.broadcast is None:
            return None
        self._pictures = max(self._pictures, after) + 1
        return StreamFrame(self._pictures, self.broadcast)

    async def _input(self, kind: str, value: Any) -> None:
        await asyncio.sleep(self.input_seconds)
        if self.fail_next:
            reason, self.fail_next = self.fail_next, None
            raise DeviceError(reason)
        self.inputs.append((kind, value))

    async def tap(self, x: float, y: float) -> None:
        await self._input("tap", (x, y))

    async def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: int,
        hold_ms: int = 0,
        path: Path | None = None,
    ) -> None:
        await self._input("swipe", (x1, y1, x2, y2, duration_ms, hold_ms, path))
        self._moving = self.fling_shots

    async def long_press(self, x: float, y: float, duration_ms: int) -> None:
        await self._input("long_press", (x, y, duration_ms))

    async def double_tap(self, x: float, y: float) -> None:
        await self._input("double_tap", (x, y))

    async def pinch(self, x: float, y: float, start: float, end: float) -> None:
        await self._input("pinch", (x, y, start, end))
        self._moving = self.fling_shots

    async def check_text_target(self, point: tuple[float, float] | None = None) -> None:
        if self.secure_field:
            raise DeviceError("secure_field")
        if point is not None and not point_in_box(point, self.field_box):
            raise WrongField(TextField("", self.field_name, self.field_box))

    async def type_text(self, text: str, *, replace: bool = False) -> None:
        await self._input("type_text", text)
        if self.drop_char is not None:
            text = text[: self.drop_char] + text[self.drop_char + 1 :]
        self.field = text if replace else self.field + text

    async def read_text_field(self, typed: str = "") -> TextField:
        if self.secure_field:
            raise DeviceError("secure_field")
        return TextField(self.field, self.field_name, self.field_box)

    async def read_sliders(self) -> list[SliderReading]:
        return list(self.sliders)

    async def select_option(self, x: float, y: float, value: str) -> Selection:
        await self._input("select_option", (x, y, value))
        if value in self.options:
            return Selection(selected=value)
        return Selection(problem="not_found")

    async def check_picker_target(self, x: float, y: float) -> None:
        if not self.picker:
            raise DeviceError("no_picker")

    async def set_picker(self, x: float, y: float, value: str) -> None:
        await self._input("set_picker", (x, y, value))

    async def back(self) -> None:
        await self._input("back", None)

    async def home(self) -> None:
        await self._input("home", None)

    async def volume_up(self) -> None:
        await self._input("volume_up", None)

    async def volume_down(self) -> None:
        await self._input("volume_down", None)

    async def lock(self) -> None:
        await self._input("lock", None)
        self.locked = True

    async def check_app(self, app_id: str) -> None:
        if self.installed_apps is not None and app_id not in self.installed_apps:
            raise DeviceError("app_not_installed")

    async def close(self) -> None:
        return None

    async def list_apps(self) -> dict[str, str | None]:
        return {app_id: None for app_id in self.installed_apps or ["com.example.app"]}

    async def launch_app(self, app_id: str) -> None:
        await self._input("launch_app", app_id)
        self.foreground_app = app_id
