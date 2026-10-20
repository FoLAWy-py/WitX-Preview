"""iOS through a local Appium server (XCUITest driver + WebDriverAgent)."""

import asyncio
import contextlib
import json
import logging
import re
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import httpx
from witx_contracts.common import Capability, Platform, Rotation
from witx_contracts.gateway import MAX_SLIDERS

from witx_gateway.devices.appium import DOWN, PRESS_MS, UP, AppiumDevice, Identities, move, pause
from witx_gateway.devices.base import DeviceError, TextField
from witx_gateway.sliders import NAME_MAX, SliderReading, parse_percent

log = logging.getLogger(__name__)

# Simulators have UUIDs; real devices have UDIDs like 00000000-0000000000000001.
SIMULATOR_UDID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$", re.I)


# WebDriverAgent answers the front app's bundle ID from a snapshot of that app's element
# tree, as deep as this setting allows (its default is 50 levels), and it places every
# point of a gesture (each move of a W3C action, a tap at a coordinate) on the app's frame,
# which it reads from such a snapshot too, once per point. On a busy screen (a chat with
# tens of thousands of elements) one snapshot takes seconds to tens of seconds, and every
# other command waits behind it; a person's drag, sampled every 16 ms, has dozens of
# points, so even a light screen's 100 ms snapshot made a drag of under a second take 4 to
# 8 s. The bundle ID and the frame need only the root, so the tree is cut to one level for
# those commands and let out again after them: finding the focused field, the sliders, the
# picker wheels and the page source all need the whole tree.
SNAPSHOT_DEPTH = 50
SNAPSHOT_DEPTH_ROOT_ONLY = 1

# An agent that lost its rights is renewed at most this often: renewing installs and starts
# it again (some 20 to 30 s), and a phone it does not help must not be renewed over and over.
RENEW_BACKOFF = 120.0

# A slider is thin: a point aimed at it on the screenshot may be this far off (points).
SLIDER_ROOM = 20.0
# Where in a step's share of the range to aim, in percent, one try after the other.
SLIDER_OFFSETS = (0.5, 0.3, 0.7, 0.4, 0.6, 0.5)
SLIDER_TRIES = len(SLIDER_OFFSETS)


class IOSDevice(AppiumDevice):
    platform: Platform = "ios"
    # A W3C swipe lets go while the finger still moves, and the list glides on.
    swipe_coasts = True
    # WebDriverAgent's own wait for the app to settle after a gesture is turned off (see
    # _session_capabilities), so the Gateway waits for the screen to rest instead.
    settles_on_its_own = False
    _name_attributes = ("label", "name", "placeholderValue")
    # iOS has no system back button; the model uses visible controls instead.
    _capabilities: ClassVar[list[Capability]] = [
        "tap",
        "swipe",
        "long_press",
        "double_tap",
        "pinch",
        "type_text",
        "set_picker",
        "select_option",
        "home",
        "lock",
        "launch_app",
        "find_app",
    ]

    def __init__(
        self,
        *,
        udid: str,
        name: str,
        appium_url: str,
        sensitive_apps: list[str] | None = None,
        ports: dict[str, int] | None = None,
        wda_url: str | None = None,
        capabilities: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        identities: Identities | None = None,
        keep_warm: bool = False,
    ) -> None:
        super().__init__(
            native_id=udid,
            name=name,
            appium_url=appium_url,
            sensitive_apps=sensitive_apps,
            ports=ports,
            capabilities=capabilities,
            transport=transport,
            identities=identities,
            keep_warm=keep_warm,
        )
        self._wda_url = wda_url
        # The screen held upright, in points, and the session it was read in (see
        # ``_screen_size``).
        self._upright: tuple[tuple[int, int], str] | None = None
        self._renewed_at = -RENEW_BACKOFF  # when its agent was last renewed (``_dark``)

    def configure(
        self,
        *,
        name: str,
        sensitive_apps: list[str],
        capabilities: dict[str, Any],
        keep_warm: bool,
        wda_url: str | None = None,
    ) -> None:
        super().configure(
            name=name, sensitive_apps=sensitive_apps, capabilities=capabilities, keep_warm=keep_warm
        )
        self._wda_url = wda_url

    @property
    def signed(self) -> bool:
        """Whether WebDriverAgent can run on a real iPhone: signed with a team, or
        already running at ``wda_url``."""
        return bool(self._wda_url or "appium:xcodeOrgId" in self._extra_capabilities)

    def _session_capabilities(self) -> dict[str, Any]:
        caps: dict[str, Any] = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            # After a gesture WebDriverAgent waits up to 2 s for the app to go idle (its
            # animation cool-off). An app that never idles (a video, a feed) makes every
            # gesture 2 s longer, and on a real iPhone XCTest, having given up on the
            # idle notification, left the driver not answering until the next command
            # came, 30 s later. The Gateway waits for the screen to rest instead
            # (``settles_on_its_own``).
            "appium:settings[animationCoolOffTimeout]": 0,
        }
        if self._wda_url:
            caps["appium:webDriverAgentUrl"] = self._wda_url
        elif self._fresh_agent:
            # The agent that runs on the phone lost its rights (``_dark``): quit it and
            # start a new one, rather than use it again as Appium otherwise would.
            caps["appium:useNewWDA"] = True
        return caps

    async def _dark(self) -> NoReturn:
        """The agent sees nothing of the screen. On a real iPhone that may be an agent that
        lost its rights while the phone was locked with a passcode, which it keeps after
        someone unlocks the phone: its screenshots fail with XCTest's code 41. Then the
        phone itself is asked (``devicectl``, outside XCTest) whether it still needs its
        passcode: if not, the agent is renewed, at most every RENEW_BACKOFF seconds, and
        the call fails as "session_lost" (the next one opens a new session). Otherwise,
        and on a simulator, the screen is locked or dark until someone wakes it."""
        if SIMULATOR_UDID.match(self.native_id) or self._wda_url:
            await super()._dark()
        try:
            await self._session_call("GET", "/screenshot")
            unauthorized = False
        except DeviceError as error:
            unauthorized = error.reason == "agent_unauthorized"
        now = time.monotonic()
        if (
            unauthorized
            and now - self._renewed_at >= RENEW_BACKOFF
            and await passcode_required(self.native_id) is False
        ):
            self._renewed_at = now
            self.locked = False
            log.warning("renewing an agent that lost its rights", extra={"device": self.native_id})
            await self._renew_agent()
            raise DeviceError("session_lost")
        await super()._dark()

    async def _identity_of(self) -> str:
        info = await self._script("mobile: deviceInfo")
        if not isinstance(info, dict):
            raise DeviceError("wrong_device")
        simulator = info.get("isSimulator") is True
        if simulator != bool(SIMULATOR_UDID.match(self.native_id)):
            raise DeviceError("wrong_device")
        if simulator:
            # A simulator is reached by its UDID, so that is who it is. What the device
            # reports as its own ID is WebDriverAgent's identifierForVendor, which iOS
            # changes when the agent is reinstalled: a new value there is not a new device.
            return f"simulator:{self.native_id}"
        udid = (info.get("lockdownInfo") or {}).get("UniqueDeviceID")
        if udid != self.native_id:
            raise DeviceError("wrong_device")
        return f"device:{udid}"

    def _outdated(self, pinned: str, seen: str) -> bool:
        # Simulators were pinned as simulator:<identifierForVendor> before.
        return pinned.startswith("simulator:") and seen.startswith("simulator:")

    @contextlib.asynccontextmanager
    async def _root_only(self) -> AsyncIterator[None]:
        """A command that needs no more of the element tree than the app's root runs with
        the snapshot cut to that; the depth is put back after it, also when it fails."""
        await self._settings(snapshotMaxDepth=SNAPSHOT_DEPTH_ROOT_ONLY)
        try:
            yield
        finally:
            await self._settings(snapshotMaxDepth=SNAPSHOT_DEPTH)

    async def _foreground_app(self) -> str:
        async with self._root_only():
            app = await self._script("mobile: activeAppInfo")
        try:
            return str(app["bundleId"])
        except (KeyError, TypeError):
            raise DeviceError("unknown_geometry") from None

    async def _touch(self, *fingers: list[dict[str, Any]]) -> None:
        async with self._root_only():
            await super()._touch(*fingers)

    async def _screen_size(self, rotation: Rotation) -> tuple[int, int]:
        """The screen in points, the way round the front app is held. Read from the screen
        itself, not from the front app's window: that would be a snapshot of the app's
        whole element tree, and a phone's app fills the screen anyway. The screen
        is read once per session (reading it takes a quarter of a second, the time to find
        the status bar) and then only turned with the rotation: WebDriverAgent turns both
        by the front app's orientation, and a phone's screen does not change size."""
        session = await self._session()
        if self._upright is None or self._upright[1] != session:
            info = await self._script("mobile: deviceScreenInfo")
            try:
                size = info["screenSize"]
                width, height = int(size["width"]), int(size["height"])
            except (KeyError, TypeError, ValueError):
                raise DeviceError("unknown_geometry") from None
            self._upright = ((min(width, height), max(width, height)), session)
        (short, long), _ = self._upright
        return (long, short) if rotation in (90, 270) else (short, long)

    async def tap(self, x: float, y: float) -> None:
        # A W3C press, like the other gestures: XCUITest's own tap (``mobile: tap``) took
        # 11 s once on a real iPhone playing video, the W3C gestures around it 3 s.
        await self._touch([move(x, y), DOWN, pause(PRESS_MS), UP])

    async def check_text_target(self, point: tuple[float, float] | None = None) -> None:
        element_id = await self._focused_element()
        kind = await self._session_call("GET", f"/element/{element_id}/attribute/type")
        if kind == "XCUIElementTypeSecureTextField":
            raise DeviceError("secure_field")
        await self._check_field_at(element_id, point)

    async def type_text(self, text: str, *, replace: bool = False) -> None:
        element_id = await self._focused_element()
        if replace:
            await self._session_call("POST", f"/element/{element_id}/clear")
        await self._session_call("POST", f"/element/{element_id}/value", {"text": text})

    async def read_text_field(self, typed: str = "") -> TextField:
        element_id = await self._focused_element()
        kind = await self._session_call("GET", f"/element/{element_id}/attribute/type")
        if kind == "XCUIElementTypeSecureTextField":
            raise DeviceError("secure_field")
        value = await self._session_call("GET", f"/element/{element_id}/attribute/value")
        text = str(value or "")
        return TextField(
            text=text,
            name=await self._field_name(element_id, self._name_attributes, typed),
            box=await self._field_box(element_id),
        )

    async def read_sliders(self) -> list[SliderReading]:
        """The sliders in view. Looked up by type, not by reading the whole UI tree: that
        takes up to seconds on a busy screen, this a few tens of milliseconds."""
        found = await self._session_call(
            "POST", "/elements", {"using": "class name", "value": "XCUIElementTypeSlider"}
        )
        readings: list[SliderReading] = []
        for slider in found[:MAX_SLIDERS] if isinstance(found, list) else []:
            element_id = next(iter(slider.values()), None) if isinstance(slider, dict) else None
            if not element_id:
                continue
            attribute = f"/element/{element_id}/attribute/"
            if await self._session_call("GET", attribute + "visible") not in ("true", True, "1", 1):
                continue
            value = str(await self._session_call("GET", attribute + "value") or "").strip()
            box = await self._field_box(str(element_id))
            if value and box is not None:
                name = await self._field_name(str(element_id), ("label", "name"))
                readings.append(SliderReading(name[:NAME_MAX], value, box))
        return sorted(readings, key=lambda s: (s.box[1], s.box[0]))

    async def _control_at(self, x: float, y: float) -> tuple[str, str]:
        """The picker wheel or slider under a point, as ("wheel" | "slider", element ID).
        A point aimed at a thin slider is given some room."""
        for kind, class_name, room in (
            ("wheel", "XCUIElementTypePickerWheel", 0.0),
            ("slider", "XCUIElementTypeSlider", SLIDER_ROOM),
        ):
            found = await self._session_call(
                "POST", "/elements", {"using": "class name", "value": class_name}
            )
            for element in found if isinstance(found, list) else []:
                element_id = (
                    next(iter(element.values()), None) if isinstance(element, dict) else None
                )
                if not element_id:
                    continue
                rect = await self._session_call("GET", f"/element/{element_id}/rect")
                try:
                    left, top = float(rect["x"]), float(rect["y"])
                    right, bottom = left + float(rect["width"]), top + float(rect["height"])
                except (KeyError, TypeError, ValueError):
                    continue
                if left - room <= x < right + room and top - room <= y < bottom + room:
                    return kind, str(element_id)
        raise DeviceError("no_picker")

    async def check_picker_target(self, x: float, y: float) -> None:
        await self._control_at(x, y)

    async def set_picker(self, x: float, y: float, value: str) -> None:
        # XCUITest scrolls the wheel to the item. A value the wheel does not offer leaves
        # it unchanged; the next screenshot shows which.
        kind, element_id = await self._control_at(x, y)
        if kind == "slider":
            await self._set_slider(element_id, value)
            return
        await self._session_call("POST", f"/element/{element_id}/value", {"text": value})

    async def _set_slider(self, element_id: str, value: str) -> None:
        """Sets a slider to the position it reports as ``value`` ("27%"). Dragging to a
        point lands on a neighbouring step about every other time, and so does XCUITest's own
        setting, but it reports where it came to rest: try again until the slider reports the
        value, a few times at most. A value that is not a percentage leaves it unchanged."""
        percent = parse_percent(value)
        if percent is None:
            return
        for offset in SLIDER_OFFSETS[:SLIDER_TRIES]:
            # In the middle of the step's share of the range, away from the rounding edge.
            position = round(min(1.0, (percent + offset) / 100), 4)
            await self._session_call(
                "POST", f"/element/{element_id}/value", {"text": str(position)}
            )
            reported = await self._session_call("GET", f"/element/{element_id}/attribute/value")
            if parse_percent(str(reported or "")) == percent:
                return

    async def back(self) -> None:
        raise DeviceError("unsupported")

    @property
    def capabilities(self) -> list[Capability]:
        """A simulator has no volume keys: XCUITest presses them on a real iPhone only."""
        if SIMULATOR_UDID.match(self.native_id):
            return list(self._capabilities)
        return [*self._capabilities, "volume_up", "volume_down"]

    async def home(self) -> None:
        await self._script("mobile: pressButton", {"name": "home"})

    async def volume_up(self) -> None:
        await self._script("mobile: pressButton", {"name": "volumeUp"})

    async def volume_down(self) -> None:
        await self._script("mobile: pressButton", {"name": "volumeDown"})

    async def check_app(self, app_id: str) -> None:
        if await self._script("mobile: isAppInstalled", {"bundleId": app_id}) is not True:
            raise DeviceError("app_not_installed")

    async def launch_app(self, app_id: str) -> None:
        await self._script("mobile: activateApp", {"bundleId": app_id})

    async def list_apps(self) -> dict[str, str | None]:
        if SIMULATOR_UDID.match(self.native_id):
            apps = await _simulator_apps(self.native_id)
        else:
            apps = {}
            for kind in ("User", "System"):
                found = await self._script(
                    "mobile: listApps",
                    {
                        "applicationType": kind,
                        "returnAttributes": ["CFBundleDisplayName", "CFBundleName"],
                    },
                )
                apps.update(found if isinstance(found, dict) else {})
        return {str(app_id): _names(info) for app_id, info in apps.items()}


def _names(info: Any) -> str | None:
    """The names an app goes by: the one under its icon, and its bundle name when that
    differs (Xiaohongshu shows "rednote" on an English iPhone)."""
    found = (info or {}).get("CFBundleDisplayName"), (info or {}).get("CFBundleName")
    names = list(dict.fromkeys(str(n) for n in found if n))
    return " · ".join(names) or None


async def passcode_required(udid: str) -> bool | None:
    """Whether a real iPhone is locked and needs its passcode now, as the phone itself says
    (``devicectl``, outside XCTest, a fraction of a second); None when it cannot be told."""
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "lock.json"
        try:
            process = await asyncio.create_subprocess_exec(
                "xcrun",
                "devicectl",
                "device",
                "info",
                "lockState",
                "--device",
                udid,
                "--json-output",
                str(out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), 10)
            result = json.loads(out.read_text()).get("result") or {}
        except (OSError, TimeoutError, ValueError):
            return None
    required = result.get("passcodeRequired")
    return required if isinstance(required, bool) else None


async def _simulator_apps(udid: str) -> dict[str, Any]:
    """Apps on a simulator, from simctl (Appium lists apps on real devices only). The
    simulator runs on this Mac, next to the Gateway."""
    try:
        listing = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "listapps", udid, stdout=asyncio.subprocess.PIPE
        )
        plist, _ = await asyncio.wait_for(listing.communicate(), 30)
        convert = await asyncio.create_subprocess_exec(
            "plutil",
            "-convert",
            "json",
            "-o",
            "-",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        data, _ = await asyncio.wait_for(convert.communicate(plist), 30)
        apps = json.loads(data)
    except (OSError, TimeoutError, ValueError):
        raise DeviceError("app_list_unavailable") from None
    if not isinstance(apps, dict):
        raise DeviceError("app_list_unavailable")
    return apps
