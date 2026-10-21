"""Shared plumbing for phones driven through a local Appium server.

Both platforms go through Appium: iOS with the XCUITest driver, Android with
UiAutomator2. This module owns the session, error mapping, screenshots and the
platform-independent parts of the device state; the subclasses add the inputs.
"""

import asyncio
import base64
import binascii
import contextlib
import itertools
import logging
import re
import time
from typing import Any, ClassVar, NoReturn, Protocol
from urllib.parse import urlsplit

import httpx
from witx_contracts.common import Capability, Platform, Rotation

from witx_gateway.devices.base import (
    DeviceError,
    DeviceState,
    Path,
    Selection,
    TextField,
    WrongField,
    point_in_box,
)
from witx_gateway.mjpeg import MjpegStream, StreamFrame
from witx_gateway.options import Option, find_option, parse_options, signature

log = logging.getLogger(__name__)

TIMEOUT = 30
# A driver command that takes longer is logged: on a real iPhone some apps' screens
# made WebDriverAgent take 15 s and more over commands that read the screen's elements.
SLOW_CALL = 2.0
ROTATIONS: dict[int, Rotation] = {0: 0, 90: 90, 180: 180, 270: 270}
# XCTest's words (XCTDaemonErrorDomain code 41) when the agent on an iPhone may not act on
# the screen: the phone is locked with a passcode, or was, and the agent running since
# then lost its rights for good (unlocking does not give them back).
AGENT_UNAUTHORIZED = "Not authorized for performing UI testing actions"
# Appium's words when the driver's agent on the device is gone (the UiAutomator2
# instrumentation ended, WebDriverAgent stopped) while the Appium session still exists.
AGENT_GONE = (
    "instrumentation process is not running",
    "Could not proxy command to the remote server",
)
# Creating a session may build and install the driver's on-device agent first
# (WebDriverAgent on iOS), which takes minutes.
SESSION_TIMEOUT = 600
# The picture the driver broadcasts for a person who has taken the phone over. Its longer
# side stays within what the live view asks for (1280): half the native size, 50, is
# 1200 px on a 2400 px tall Android screen and about 1270 on an iPhone.
STREAM_SETTINGS: dict[str, Any] = {
    "appium:settings[mjpegServerFramerate]": 30,
    "appium:settings[mjpegScalingFactor]": 50,
    "appium:settings[mjpegServerScreenshotQuality]": 60,
}


class Identities(Protocol):
    """Where pinned device identities are kept (the Gateway's journal)."""

    def pinned_identity(self, native_id: str) -> str | None: ...

    def pin_identity(self, native_id: str, identity: str) -> None: ...

    def identity_changed(self, native_id: str, seen: str) -> None: ...

    def changed_identity(self, native_id: str) -> str | None: ...

    def forget_identity(self, native_id: str) -> bool: ...


# Looking for an option in a list: how long, how far each swipe goes, and how long a
# swipe may take to let the list rest before it is read.
SELECT_BUDGET = 40.0
SELECT_SWIPES = 80
SWIPE_SPAN = 0.4  # of the screen height: less than a screenful, so no option is skipped
REST_POLL = 0.15
REST_MAX = 2.0
# A scroll that holds still this long before it lets go has no speed left, so the list stops
# where the finger does instead of coasting on past the option.
SCROLL_STOP_MS = 150
# A double tap: each press this long, this far apart. Both platforms take two taps as one
# double tap when the second comes within 300 ms, and Android not sooner than 40 ms.
PRESS_MS = 50  # a tap, and each press of a double tap
DOUBLE_TAP_GAP_MS = 100
# A pinch's fingers move in steps of this long. UiAutomator2 plays one long move of two
# fingers as a jump, which apps do not take as a pinch, and refuses steps much shorter.
# The fingers then rest this long before they lift: a map lifted sooner zooms on (with a
# 150 ms rest Google Maps went on for up to 6 s, with 400 ms for at most 2 s).
PINCH_STEPS = 16
PINCH_STEP_MS = 25
PINCH_REST_MS = 400
# One finger, for W3C actions.
FINGER: dict[str, Any] = {"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}}
DOWN: dict[str, Any] = {"type": "pointerDown", "button": 0}
UP: dict[str, Any] = {"type": "pointerUp", "button": 0}


def move(x: float, y: float, duration_ms: int = 0) -> dict[str, Any]:
    return {"type": "pointerMove", "duration": duration_ms, "x": round(x), "y": round(y)}


def pause(duration_ms: int) -> dict[str, Any]:
    return {"type": "pause", "duration": duration_ms}


class AppiumDevice:
    _name_attributes: tuple[str, ...] = ()  # what a field's name is read from, in order
    platform: Platform
    _capabilities: ClassVar[list[Capability]]

    def __init__(
        self,
        *,
        native_id: str,
        name: str,
        appium_url: str,
        sensitive_apps: list[str] | None = None,
        ports: dict[str, int] | None = None,
        capabilities: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        identities: Identities | None = None,
        keep_warm: bool = False,
    ) -> None:
        if urlsplit(appium_url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Appium must run on this host (loopback URL)")
        self.native_id = native_id
        self.name = name
        self.appium_url = appium_url.rstrip("/")
        self.sensitive_apps = sensitive_apps or []
        self._session_id: str | None = None  # opened on first use, reopened when lost
        self._opening = asyncio.Lock()  # one session is opened at a time
        # A dedicated pool phone: its session, and the agent on the phone behind it, is kept
        # open without waiting for a task (see ``warm``), so that nobody waits for it to start.
        self.keep_warm = keep_warm
        # The geometry last read: a screen broadcast starts with it instead of reading it
        # first (on a real iPhone that read can take as long as opening the session).
        self.last_state: DeviceState | None = None
        # Its screen was found locked or dark the last time it was looked at: reported as
        # the problem "locked" until a look finds it on again.
        self.locked = False
        self._ports = ports or {}  # from ports.assign: never shared with another device
        self._extra_capabilities = capabilities or {}  # e.g. WebDriverAgent signing
        self._identity: str | None = None  # the device at the other end, pinned when first seen
        self._identities = identities  # pins kept across restarts (the journal)
        self._http = httpx.AsyncClient(base_url=appium_url, timeout=TIMEOUT, transport=transport)
        port = self._ports.get("appium:mjpegServerPort")
        self._stream = (
            MjpegStream(f"http://127.0.0.1:{port}", transport=transport) if port else None
        )

    def configure(
        self,
        *,
        name: str,
        sensitive_apps: list[str],
        capabilities: dict[str, Any],
        keep_warm: bool,
    ) -> None:
        """Applies the device's settings from the Gateway's configuration while it runs:
        what the next report says and the next session is opened with. A session
        that is open stays as it is, so a device in the middle of a task is not touched."""
        self.name = name
        self.sensitive_apps = list(sensitive_apps)
        self._extra_capabilities = dict(capabilities)
        self.keep_warm = keep_warm

    def _session_capabilities(self) -> dict[str, Any]:
        """Driver capabilities for a new session (without the common ones)."""
        raise NotImplementedError

    async def _foreground_app(self) -> str:
        raise NotImplementedError

    async def _screen_size(self, rotation: Rotation) -> tuple[int, int]:
        """The input space, as width and height the way round the screen is held now
        (``rotation``): the front window's size, which the driver reports in the device's
        input units."""
        rect = await self._session_call("GET", "/window/rect")
        try:
            return int(rect["width"]), int(rect["height"])
        except (KeyError, TypeError, ValueError):
            raise DeviceError("unknown_geometry") from None

    async def _settings(self, **settings: Any) -> None:
        """Changes the driver's settings for this session."""
        await self._session_call("POST", "/appium/settings", {"settings": settings})

    async def _identity_of(self) -> str:
        """Who answers this session, as the on-device agent reports it. Raises
        DeviceError("wrong_device") if it plainly is not ``native_id``."""
        raise NotImplementedError

    @property
    def identity_changed(self) -> bool:
        """Another device answered for ``native_id`` than the one first seen: refused until
        a person confirms it (``witx-gateway forget-device``)."""
        return bool(self._identities and self._identities.changed_identity(self.native_id))

    async def _check_identity(self) -> None:
        """The session must keep reaching the same device. Two drivers that share a local
        port (WebDriverAgent on 8100, say) can silently send one device's input to another
        of the same size; geometry checks cannot tell them apart. The first identity seen
        is kept in the journal, so a restart, or another device that later takes the same
        serial (an emulator on the same port), cannot quietly replace it."""
        identity = await self._identity_of()
        if self._identity is None and self._identities is not None:
            self._identity = self._identities.pinned_identity(self.native_id)
            if self._identity is None:
                self._identities.pin_identity(self.native_id, identity)
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            if self._identities is not None and self._outdated(self._identity, identity):
                self._identities.forget_identity(self.native_id)
                self._identities.pin_identity(self.native_id, identity)
                self._identity = identity
                return
            if self._identities is not None:
                self._identities.identity_changed(self.native_id, identity)
            raise DeviceError("wrong_device")

    def _outdated(self, pinned: str, seen: str) -> bool:
        """Whether a pin kept in the journal was written in a format no longer used, so a
        different value is no sign of a different device."""
        return False

    async def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = TIMEOUT,
    ) -> Any:
        started = time.monotonic()
        try:
            response = await self._http.request(method, path, json=body, timeout=timeout)
        except httpx.TimeoutException:
            self._log_slow(method, path, body, started, "timeout")
            raise DeviceError("timeout") from None
        except httpx.TransportError:
            raise DeviceError("appium_unavailable") from None
        if time.monotonic() - started >= SLOW_CALL:
            self._log_slow(method, path, body, started, "answered")
        try:
            payload = response.json()
        except ValueError:
            raise DeviceError("appium_bad_response") from None
        value = payload.get("value") if isinstance(payload, dict) else None
        if response.status_code != 200 or (isinstance(value, dict) and "error" in value):
            error = value.get("error") if isinstance(value, dict) else None
            if error == "invalid session id":
                # The driver restarted or the session expired: the next call opens a new
                # one. The current call still fails; nothing is retried here.
                self._session_id = None
                raise DeviceError("session_lost")
            message = str(value.get("message", "")) if isinstance(value, dict) else ""
            if any(sign in message for sign in AGENT_GONE):
                # The session exists but cannot reach the device: end it, so the next
                # call opens a fresh one (and its agent). This call still fails.
                await self._end_session()
                raise DeviceError("session_lost")
            if AGENT_UNAUTHORIZED in message:
                raise DeviceError("agent_unauthorized")
            raise DeviceError("appium_error")
        return value

    def _log_slow(
        self, method: str, path: str, body: dict[str, Any] | None, started: float, outcome: str
    ) -> None:
        """Logs a driver command that was slow or timed out: which one (a script by
        its name, never its arguments, which may be typed text), how long it took, and the
        foreground app last read. That app is not read again: the read may be what hangs.
        A gesture also says how many steps it had and how long they were meant to take
        (never where): a drag takes at least as long as the finger did. A settings change
        says which: it only writes a number into the driver, so when it is slow the driver
        was busy with something else, before or after our command."""
        command = re.sub(r"^/session/[^/]+", "", path) or path
        gesture: dict[str, int] = {}
        if command == "/execute/sync" and body:
            command = str(body.get("script"))
        else:
            command = f"{method} {command}"
        if command == "POST /actions" and body:
            steps = [s for finger in body.get("actions", []) for s in finger.get("actions", [])]
            gesture = {
                "steps": len(steps),
                "planned_ms": sum(int(s.get("duration") or 0) for s in steps),
            }
        elif command == "POST /appium/settings" and body:
            # Which settings change it was (the values are numbers and flags): the one
            # before a command or the one after it says what the driver was busy with.
            gesture = dict(body.get("settings") or {})
        log.warning(
            "slow driver command",
            extra={
                "device": self.native_id,
                "command": command,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "outcome": outcome,
                "foreground_app": self.last_state.foreground_app if self.last_state else None,
                **gesture,
            },
        )

    async def close(self) -> None:
        """Ends the session this Gateway opened (on shutdown)."""
        if self._stream is not None:
            await self._stream.close()
        await self._end_session()

    async def _end_session(self) -> None:
        session_id, self._session_id = self._session_id, None
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                await self._http.delete(f"/session/{session_id}", timeout=TIMEOUT)

    @property
    def session_open(self) -> bool:
        return self._session_id is not None

    async def _session(self) -> str:
        async with self._opening:  # two callers must not each open one for the same phone
            if self._session_id is None:
                self._session_id = await self._open_session()
            return self._session_id

    async def warm(self) -> None:
        """Keeps the session ready: opens it if there is none (the driver starts its agent on
        the phone with it, several seconds on a real iPhone), else checks that it still
        answers. A session that was lost is dropped here, and opened again by the next
        call."""
        if self._session_id is None:
            await self._session()
            await self.state()  # reads the geometry once, so that the first picture need not
        else:
            await self.check_lock()

    async def check_lock(self) -> None:
        """Looks whether the screen is locked or dark, cheaply: two commands that read no
        element tree. Raises DeviceError("locked") and remembers it (``locked``); a screen
        that is on clears it."""
        if await self._script("mobile: isLocked") is not False:
            self.locked = True
            raise DeviceError("locked")
        await self._rotation(await self._session_call("GET", "/rotation"))
        self.locked = False

    async def _rotation(self, rotation: Any) -> Rotation:
        """The screen's turn, from the driver's rotation. A rotation of -1 (WebDriverAgent:
        the front app's orientation is unknown) means the agent sees nothing of the
        screen (``_dark``)."""
        try:
            degrees = int(rotation["z"])
        except (KeyError, TypeError, ValueError):
            raise DeviceError("unknown_geometry") from None
        if degrees < 0:
            await self._dark()
        if degrees % 360 not in ROTATIONS:
            raise DeviceError("unknown_geometry")
        return ROTATIONS[degrees % 360]

    async def _dark(self) -> NoReturn:
        """The agent sees nothing of the screen: it is dark or locked, until someone wakes
        or unlocks it. Raises DeviceError("locked") and remembers it."""
        self.locked = True
        raise DeviceError("locked")

    async def wake(self) -> None:
        """Wakes the screen for a person taking the phone over: both drivers' unlock, which
        lights the screen and, on a phone with no passcode, opens it (iOS presses Home,
        Android dismisses a simple keyguard). A passcode stays for the person; a phone that
        cannot be woken says so at the next look."""
        await self._script("mobile: unlock")

    @property
    def capabilities(self) -> list[Capability]:
        return list(self._capabilities)

    async def lock(self) -> None:
        """Both drivers' lock: the screen goes off, as with the side key."""
        await self._script("mobile: lock")

    async def _open_session(self) -> str:
        caps: dict[str, Any] = {
            "appium:udid": self.native_id,
            "appium:noReset": True,
            # The Gateway may sit idle between tasks; keep the session open.
            "appium:newCommandTimeout": 0,
            **self._session_capabilities(),
            **self._ports,
            **(STREAM_SETTINGS if self._stream else {}),
            **self._extra_capabilities,
        }
        value = await self._call(
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": caps}},
            timeout=SESSION_TIMEOUT,
        )
        return str(value["sessionId"])

    async def _session_call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        return await self._call(method, f"/session/{await self._session()}{path}", body)

    async def _script(self, script: str, args: dict[str, Any] | None = None) -> Any:
        return await self._session_call(
            "POST", "/execute/sync", {"script": script, "args": [args] if args else []}
        )

    async def state(self) -> DeviceState:
        await self._check_identity()
        if await self._script("mobile: isLocked") is not False:
            self.locked = True
            raise DeviceError("locked")
        # The rotation, not the orientation: both ways round are just "LANDSCAPE" there,
        # and a phone turned over must not take taps meant for the other way round. Read
        # before the front app: a dark screen shows here at once, while the front
        # app of a dark screen takes seconds to say nothing.
        rotation = await self._rotation(await self._session_call("GET", "/rotation"))
        app = await self._foreground_app()
        width, height = await self._screen_size(rotation)
        self.locked = False
        self.last_state = DeviceState(width, height, rotation, app)
        return self.last_state

    async def screenshot(self) -> bytes:
        encoded = await self._session_call("GET", "/screenshot")
        try:
            return base64.b64decode(str(encoded), validate=True)
        except (ValueError, binascii.Error):
            raise DeviceError("invalid_screenshot") from None

    async def stream_frame(self, after: int, wait: float) -> StreamFrame | None:
        """The newest picture of the driver's screen broadcast, once it is newer than
        ``after``; None when this device has no broadcast or it is not working."""
        if self._stream is None:
            return None
        await self._session()  # the driver starts the broadcast with its session
        return await self._stream.next(after, wait)

    async def _field_name(
        self, element_id: str, attributes: tuple[str, ...], typed: str = ""
    ) -> str:
        """What the app calls the field: the first of these attributes it fills in, without
        the text the field holds (Compose fields describe themselves as "<text>, <name>")."""
        for attribute in attributes:
            value = await self._session_call("GET", f"/element/{element_id}/attribute/{attribute}")
            if not isinstance(value, str):
                continue
            name = value.replace(typed, "") if typed.strip() else value
            name = name.strip(" ,;:\n\t")
            if name not in ("", "null"):  # "null": the attribute has no value
                return name[:200]
        return ""

    async def tap(self, x: float, y: float) -> None:
        raise NotImplementedError

    # Gestures are W3C actions on both platforms, so a duration means the same on each: the
    # time the finger takes to move. (XCUITest's own drag, ``mobile: dragFromToForDuration``,
    # takes its duration as how long to press at the start before a quick drag, #205.)

    async def _touch(self, *fingers: list[dict[str, Any]]) -> None:
        """One W3C action per finger, all at the same time."""
        sources = [
            FINGER | {"id": f"finger{n or ''}", "actions": steps} for n, steps in enumerate(fingers)
        ]
        await self._session_call("POST", "/actions", {"actions": sources})

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
        # A path is replayed as one move per point, each taking the time between its point
        # and the one before: a person's drag with its pauses and speed, not a line.
        steps = [move(x1, y1), DOWN]
        if path is None:
            steps.append(move(x2, y2, duration_ms))
        else:
            for (_, _, at), (bx, by, bt) in itertools.pairwise(path):
                steps.append(move(bx, by, bt - at))
        if hold_ms:
            steps.append(pause(hold_ms))
        await self._touch([*steps, UP])

    async def long_press(self, x: float, y: float, duration_ms: int) -> None:
        await self._touch([move(x, y), DOWN, pause(duration_ms), UP])

    async def double_tap(self, x: float, y: float) -> None:
        press = [DOWN, pause(PRESS_MS), UP]
        await self._touch([move(x, y), *press, pause(DOUBLE_TAP_GAP_MS), *press])

    async def pinch(self, x: float, y: float, start: float, end: float) -> None:
        fingers = []
        for side in (-1, 1):
            path = [
                move(x + side * (start + (end - start) * k / PINCH_STEPS), y, PINCH_STEP_MS)
                for k in range(1, PINCH_STEPS + 1)
            ]
            fingers.append([move(x + side * start, y), DOWN, *path, pause(PINCH_REST_MS), UP])
        await self._touch(*fingers)

    async def _options(self) -> list[Option]:
        return parse_options(str(await self._session_call("GET", "/source") or ""))

    async def _resting_options(self) -> list[Option]:
        """What the screen shows once it has stopped moving: two reads in a row agree."""
        options = await self._options()
        deadline = time.monotonic() + REST_MAX
        while time.monotonic() < deadline:
            await asyncio.sleep(REST_POLL)
            newer = await self._options()
            resting = signature(newer) == signature(options)
            options = newer
            if resting:
                break
        return options

    async def select_option(self, x: float, y: float, value: str) -> Selection:
        """Looks for the option on the screen; if it is not there, scrolls the list around
        (x, y) down to its end, then up to its start, a little less than a screenful at a
        time, and taps the option when exactly one shows ``value``. Scrolling changes
        nothing but what is in view, so running out of time or list ends in "not_found"
        without a tap."""
        state = await self.state()
        screen = (state.input_width, state.input_height)
        deadline = time.monotonic() + SELECT_BUDGET
        span = SWIPE_SPAN * screen[1]
        top = min(max(y - span / 2, 0.05 * screen[1]), 0.95 * screen[1] - span)

        async def look() -> tuple[str, Option | None, list[Option]]:
            options = await self._resting_options()
            verdict, found = find_option(options, value, screen)
            return verdict, found, options

        verdict, found, options = await look()
        swipes = 0
        for forward in (True, False):  # down the list to its end, then back up to its start
            while verdict == "missing" and swipes < SELECT_SWIPES and time.monotonic() < deadline:
                before = signature(options)
                start, end = (top + span, top) if forward else (top, top + span)
                await self.swipe(x, start, x, end, 600, hold_ms=SCROLL_STOP_MS)
                swipes += 1
                verdict, found, options = await look()
                if signature(options) == before:  # it did not move: the end of the list
                    break
        if verdict == "found" and found is not None:
            await self.tap(*found.center)
            return Selection(selected=found.text)
        return Selection(problem="ambiguous" if verdict == "ambiguous" else "not_found")

    async def _check_field_at(self, element_id: str, point: tuple[float, float] | None) -> None:
        """The focused field must be where the agent said (WrongField, with the field that
        has the focus, if not). Nothing is checked when it said nothing or the box cannot
        be read."""
        if point is None:
            return
        box = await self._field_box(element_id)
        if not point_in_box(point, box):
            raise WrongField(
                TextField(
                    text="",
                    name=await self._field_name(element_id, self._name_attributes, ""),
                    box=box,
                )
            )

    async def _field_box(self, element_id: str) -> tuple[float, float, float, float] | None:
        rect = await self._session_call("GET", f"/element/{element_id}/rect")
        try:
            return (float(rect["x"]), float(rect["y"]), float(rect["width"]), float(rect["height"]))
        except (KeyError, TypeError, ValueError):
            return None

    async def _focused_element(self) -> str:
        try:
            element = await self._session_call("GET", "/element/active")
        except DeviceError as error:
            if error.reason == "appium_error":
                raise DeviceError("no_focused_field") from None
            raise
        element_id = next(iter(element.values()), None) if isinstance(element, dict) else None
        if not element_id:
            raise DeviceError("no_focused_field")
        return str(element_id)
