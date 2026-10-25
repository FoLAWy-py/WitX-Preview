"""Android through a local Appium server (UiAutomator2 driver).

Coordinates are screen pixels. Text goes in at the cursor as one paste: `mobile: type`
takes any Unicode text too, but sends it key by key, and a field that reformats as it
receives (a phone number box) loses characters when they arrive that fast.
"""

import base64
import io
import re
from typing import Any, ClassVar

import httpx
from PIL import Image, UnidentifiedImageError
from witx_contracts.common import Capability, Platform, TypeMethod

from witx_gateway.devices.appium import AppiumDevice, Identities
from witx_gateway.devices.base import DeviceError, TextField
from witx_gateway.sliders import SliderReading, parse_android_sliders

# A window with FLAG_SECURE (banking, payment, incognito) shows up black in screenshots.
BLANK_SHARE = 0.99
BLANK_LEVEL = 16
# UiAutomator2 waits for the app to go idle before reading state, so the screenshot
# after an input shows the settled screen (1-3 s on the emulator). Its default limit is
# 10 s, which a screen that never settles (video, carousel, spinner) would hit on every
# step; past this limit the Gateway goes ahead with the screen as it is.
IDLE_TIMEOUT_MS = 3000
KEYCODE_PASTE = 279
KEYCODE_A = 29
META_CTRL = 0x3000  # META_CTRL_ON | META_CTRL_LEFT_ON
_APP_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")


class AndroidDevice(AppiumDevice):
    platform: Platform = "android"
    swipe_coasts = True
    settles_on_its_own = True  # waitForIdleTimeout below
    # Text is always pasted: "paste" is what "set" does. No keys: a key at a time
    # loses characters in fields that format as they go.
    type_methods: frozenset[TypeMethod] = frozenset({"set", "paste"})
    _name_attributes = ("hint", "content-desc", "resource-id")
    # No picker capability: Android pickers are ordinary scrolling lists or text fields.
    _capabilities: ClassVar[list[Capability]] = [
        "tap",
        "swipe",
        "long_press",
        "double_tap",
        "pinch",
        "type_text",
        "select_option",
        "back",
        "home",
        "volume_up",
        "volume_down",
        "lock",
        "launch_app",
        "find_app",
    ]

    def __init__(
        self,
        *,
        serial: str,
        name: str,
        appium_url: str,
        sensitive_apps: list[str] | None = None,
        ports: dict[str, int] | None = None,
        capabilities: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        identities: Identities | None = None,
        keep_warm: bool = False,
    ) -> None:
        super().__init__(
            native_id=serial,
            name=name,
            appium_url=appium_url,
            sensitive_apps=sensitive_apps,
            ports=ports,
            capabilities=capabilities,
            transport=transport,
            identities=identities,
            keep_warm=keep_warm,
        )

    def _session_capabilities(self) -> dict[str, Any]:
        return {
            "platformName": "Android",
            "appium:automationName": "UiAutomator2",
            "appium:waitForIdleTimeout": IDLE_TIMEOUT_MS,
            # Without this the Settings sliders are not in the page source.
            "appium:settings[allowInvisibleElements]": True,
        }

    async def _identity_of(self) -> str:
        info = await self._script("mobile: deviceInfo")
        android_id = info.get("androidId") if isinstance(info, dict) else None
        if not android_id:
            raise DeviceError("wrong_device")
        return str(android_id)

    async def _foreground_app(self) -> str:
        package = await self._script("mobile: getCurrentPackage")
        if not isinstance(package, str) or not package:
            raise DeviceError("no_foreground_app")
        return package

    async def tap(self, x: float, y: float) -> None:
        await self._script("mobile: clickGesture", {"x": x, "y": y})

    async def read_sliders(self) -> list[SliderReading]:
        return parse_android_sliders(str(await self._session_call("GET", "/source") or ""))

    async def _password(self, element_id: str) -> bool:
        password = await self._session_call("GET", f"/element/{element_id}/attribute/password")
        return str(password).lower() == "true"

    async def check_text_target(self, point: tuple[float, float] | None = None) -> None:
        element_id = await self._focused_element()
        if await self._password(element_id) or _blank(await self.screenshot()):
            raise DeviceError("secure_field")
        await self._check_field_at(element_id, point)

    async def type_text(
        self, text: str, *, replace: bool = False, method: TypeMethod = "set", person: bool = False
    ) -> str | None:
        # "set" pastes here too: typing in key by key loses characters.
        # Read again just before the agent types: the focus can move to a password field
        # after ``check_text_target`` (a form going on). A person taking over may type one.
        # What fails while it only reads has typed nothing, and says why instead of raising.
        try:
            element_id = await self._focused_element()  # never type without a focused field
            if not person and await self._password(element_id):
                return "secure_field"
            before = await self._field_text(element_id)
        except DeviceError as error:
            return error.reason
        if replace:
            # Select all, so that the paste replaces it in one edit. Clearing the element
            # first (UiAutomator2's clear) races the paste in a web page's field.
            await self._script("mobile: pressKey", {"keycode": KEYCODE_A, "metastate": META_CTRL})
        content = base64.b64encode(text.encode()).decode()
        await self._script("mobile: setClipboard", {"content": content, "contentType": "plaintext"})
        try:
            await self._script("mobile: pressKey", {"keycode": KEYCODE_PASTE})
        finally:  # the text must not stay on the clipboard for other apps to read
            await self._script("mobile: setClipboard", {"content": "", "contentType": "plaintext"})
        # A field that refuses paste is left as it was, so typing the text in cannot
        # double it; one that already shows the text cannot tell the two apart.
        shown = before.strip() == text.strip() if replace else text.strip() in before
        if not shown and await self._field_text(element_id) == before:
            await self._script("mobile: type", {"text": text})
        return None

    async def _field_text(self, element_id: str) -> str:
        return str(await self._session_call("GET", f"/element/{element_id}/text") or "")

    async def read_text_field(self, typed: str = "") -> TextField:
        element_id = await self._focused_element()
        if await self._password(element_id):
            raise DeviceError("secure_field")
        text = await self._field_text(element_id)
        return TextField(
            text=text,
            name=await self._field_name(element_id, self._name_attributes, typed),
            box=await self._field_box(element_id),
        )

    async def check_picker_target(self, x: float, y: float) -> None:
        raise DeviceError("unsupported")

    async def set_picker(self, x: float, y: float, value: str) -> None:
        raise DeviceError("unsupported")

    async def back(self) -> None:
        await self._script("mobile: pressKey", {"keycode": 4})

    async def home(self) -> None:
        await self._script("mobile: pressKey", {"keycode": 3})

    async def volume_up(self) -> None:
        await self._script("mobile: pressKey", {"keycode": 24})

    async def volume_down(self) -> None:
        await self._script("mobile: pressKey", {"keycode": 25})

    async def check_app(self, app_id: str) -> None:
        if not _APP_ID.fullmatch(app_id):
            raise DeviceError("invalid_app_id")
        if await self._script("mobile: isAppInstalled", {"appId": app_id}) is not True:
            raise DeviceError("app_not_installed")

    async def launch_app(self, app_id: str) -> None:
        if not _APP_ID.fullmatch(app_id):
            raise DeviceError("invalid_app_id")
        await self._script("mobile: activateApp", {"appId": app_id})

    async def list_apps(self) -> dict[str, str | None]:
        # Android reports package names only; the agent searches with English words.
        apps = await self._script("mobile: listApps", {})
        if not isinstance(apps, dict):
            raise DeviceError("app_list_unavailable")
        return {str(app_id): None for app_id in apps}


def _blank(png: bytes) -> bool:
    """Whether the screenshot is (almost) entirely black."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            grey = image.convert("L")
            grey.thumbnail((128, 128))
            histogram = grey.histogram()
    except (OSError, UnidentifiedImageError):
        raise DeviceError("invalid_screenshot") from None
    return sum(histogram[:BLANK_LEVEL]) >= BLANK_SHARE * sum(histogram)
