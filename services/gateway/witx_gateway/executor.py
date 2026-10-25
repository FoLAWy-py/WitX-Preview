"""Executes cloud commands on local devices, one writer per device.

Checks before any input, in order: known device, current epoch, no stop barrier,
not expired, supported input, an installed app, the screen still matches the screenshot
the coordinates were chosen on, and a picker wheel lies under a picker input. Only
then is ``intent`` journaled and the device touched.

``find_app`` only reads the list of installed apps: it answers after the first checks
and never journals intent, so it cannot leave an unknown effect behind.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from witx_contracts import gateway as wire
from witx_contracts.common import (
    Back,
    DoubleTap,
    FindApp,
    Home,
    LaunchApp,
    Lock,
    LongPress,
    Pinch,
    SelectOption,
    SetPicker,
    Swipe,
    Tap,
    TypeText,
    VolumeDown,
    VolumeUp,
)

from witx_gateway import apps
from witx_gateway.devices.base import (
    Device,
    DeviceError,
    DeviceState,
    Selection,
    TextField,
    WrongField,
    fit_jpeg,
    fit_png,
    pinch_fingers,
    png_size,
    same_screen,
    to_jpeg,
)
from witx_gateway.journal import Journal

log = logging.getLogger(__name__)

INPUT_TIMEOUT = 30
# Text tapped in on the keyboard (up to KEYS_MOST characters, a few a second) or pasted
# into a field emptied first with the delete key held, a few seconds a round.
KEYS_TIMEOUT = 120
# Looking for an option in a long list scrolls for up to devices.appium.SELECT_BUDGET.
SELECT_TIMEOUT = 60
# A list can keep moving after a swipe lets go of it (a fling). The screenshot that
# follows must show where it came to rest, or the agent aims its next tap at an item that
# has moved on: on Android 8 of 10 shots taken at once were still moving. Wait until two
# shots in a row agree, but not for a screen that never rests (video, a carousel).
# A phone whose driver answers an input before the app has settled (iOS, D64) gets this
# after every input, not only a swipe: a tap's transition is still playing when it answers.
SETTLE_POLL = 0.1
SETTLE_MAX = 1.5
# A map goes on zooming after a pinch and then draws its tiles: on the simulators it came
# to rest 1 to 4 s after the fingers lifted, and a shot taken sooner showed it halfway,
# so that the agent zoomed again by what was already coming.
PINCH_SETTLE_MAX = 4.0
# Reading the field back after typing is best effort and must not hold the device.
READ_BACK_TIMEOUT = 5
# Reading the sliders in view for each screenshot is best effort as well.
SLIDERS_TIMEOUT = 3
# A person taking a phone over sees its own screen broadcast (mjpeg.py). Its pictures are
# tagged with the phone's geometry, which is read once and then only this rarely: reading
# it is a handful of WebDriverAgent commands, and on a real iPhone playing video each of
# them stalls the broadcast for a second or more (every second: 17 instead of 28 pictures
# a second, with stops of 1 to 5 s). A turn of the phone shows in the picture's shape and
# makes a fresh read (``_broadcast``); an input still checks the phone as it is then.
STREAM_STATE_TTL = 300.0
# Reading it has also hung for tens of seconds on a real iPhone, so a picture never
# waits for it once it is known: it is shown with the geometry last read, which is refreshed
# in the background, for at most this long, and after one that failed not again for
# STREAM_STATE_BACKOFF seconds (a hung read leaves the phone's queue full).
STREAM_STATE_TIMEOUT = 3.0
STREAM_STATE_BACKOFF = 10.0
# How long to wait for a picture newer than the last one sent.
STREAM_WAIT = 1.0
# A locked or dark phone a person takes over is woken once (both drivers' unlock), and not
# again for WAKE_BACKOFF seconds: a phone with a passcode stays locked, and waking it over
# and over would only keep lighting its screen. Waking with a passcode set waits for the
# unlock that does not come: at most WAKE_TIMEOUT.
WAKE_BACKOFF = 30.0
WAKE_TIMEOUT = 8.0
# What a person may do during a takeover: a click, a held click and a drag on the picture,
# typed text, the system buttons and the side keys.
MANUAL_INPUTS = (Tap, LongPress, Swipe, TypeText, Back, Home, VolumeUp, VolumeDown, Lock)

Send = Callable[[str], Awaitable[None]]
Upload = Callable[[UUID, bytes], Awaitable[None]]


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def at_rest(device: Device, data: bytes, limit: float) -> bytes:
    """The screenshot once the screen has stopped moving: the last of two in a row that
    agree, or the last one taken when ``limit`` seconds are up."""
    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(SETTLE_POLL)
        newer = await device.screenshot()
        resting = same_screen(data, newer)
        data = newer
        if resting:
            break
    return data


async def capture(device: Device, *, settle: float = 0.0) -> tuple[bytes, wire.ScreenState]:
    """A screenshot plus the state it was taken in. Fails if the screen changed meanwhile.
    ``settle`` waits up to that many seconds for a screen still moving (a gliding list, a
    zooming map) to come to rest first."""
    before = await device.state()
    data = await device.screenshot()
    if settle:
        data = await at_rest(device, data, settle)
    after = await device.state()
    if before != after and settle:
        # The screen changed while it settled (a home press takes an app away, an app
        # opens): the settled screen is captured once more, between two reads of its own.
        before, data = after, await device.screenshot()
        after = await device.state()
    if before != after:
        raise DeviceError("screen_changed")
    data = fit_png(data)
    width, height = png_size(data)
    # Screenshot and input space must have the same shape; mapping is then a pure scale.
    if abs(width * before.input_height - height * before.input_width) > max(width, height):
        raise DeviceError("geometry_mismatch")
    return data, wire.ScreenState(
        width=width,
        height=height,
        input_width=before.input_width,
        input_height=before.input_height,
        rotation=before.rotation,
        foreground_app=before.foreground_app,
    )


def to_input(x: float, y: float, screen: wire.ScreenState) -> tuple[float, float]:
    """Maps screenshot pixels to device input coordinates."""
    if not (0 <= x < screen.width and 0 <= y < screen.height):
        raise Rejected("out_of_bounds")
    return x * screen.input_width / screen.width, y * screen.input_height / screen.height


def same_geometry(state: DeviceState, screen: wire.ScreenState) -> bool:
    """Coordinates of the screenshot still land where they were meant: same input space,
    turned the same way (both ways round have the same size)."""
    return (state.input_width, state.input_height, state.rotation) == (
        screen.input_width,
        screen.input_height,
        screen.rotation,
    )


class Executor:
    def __init__(self, journal: Journal, devices: list[Device], send: Send, upload: Upload) -> None:
        self.journal = journal
        self.by_native = {d.native_id: d for d in devices}
        self.by_id: dict[UUID, Device] = {}
        self.send = send
        self.upload = upload
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._streamed: dict[UUID, int] = {}  # the last broadcast picture sent, per device
        # Pictures of one device are asked for several at a time (the cloud keeps requests in
        # flight): they are answered one at a time, in order, each newer than the last.
        self._streaming: dict[UUID, asyncio.Lock] = {}
        self._states: dict[UUID, tuple[float, DeviceState]] = {}  # geometry for the broadcast
        self._refreshing: dict[UUID, asyncio.Task[None]] = {}  # a geometry read under way
        self._state_failed_at: dict[UUID, float] = {}
        self._woken_at: dict[UUID, float] = {}  # when a locked phone was last woken

    def know(self, devices: list[Device]) -> None:
        """Adds devices the Gateway found after it started (plugged in later)."""
        for device in devices:
            self.by_native.setdefault(device.native_id, device)

    def bind(self, bindings: list[wire.DeviceBinding]) -> None:
        """The cloud's IDs for the devices: from the welcome, and later for new devices."""
        self.by_id.update(
            {
                b.device_id: self.by_native[b.native_id]
                for b in bindings
                if b.native_id in self.by_native
            }
        )

    def _lock(self, device_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    async def _emit(self, ref_id: UUID, message: wire.GatewayMessage) -> None:
        raw = message.model_dump_json()
        self.journal.enqueue(ref_id, raw)
        await self.send(raw)

    def recover(self) -> None:
        """Run once at start-up: settle actions a previous process left unfinished."""
        for action in self.journal.interrupted():
            outcome, reason = (
                ("unknown", "gateway_restarted")
                if action.state == "intent"
                else ("rejected", "gateway_restarted")
            )
            log.warning("action %s was interrupted; reporting %s", action.action_id, outcome)
            result = wire.ActionResult(
                action_id=action.action_id,
                outcome=outcome,
                reason=reason,
                finished_at=datetime.now(UTC),
            )
            self.journal.finish(action.action_id, outcome, reason, result.model_dump_json())

    # ------------------------------------------------------------ commands

    def lease(self, message: wire.Lease) -> None:
        self.journal.set_epoch(message.device_id, message.epoch)

    async def observe(self, message: wire.Observe) -> None:
        if not self.journal.mark_seen(message.observation_id):
            previous = self.journal.outbox_message(message.observation_id)
            if previous:
                await self.send(previous)
            return
        device = self.by_id.get(message.device_id)
        async with self._lock(message.device_id):
            result = await self._observe(device, message.observation_id)
        await self._emit(message.observation_id, result)

    async def _observe(
        self, device: Device | None, observation_id: UUID, *, settle: float = 0.0
    ) -> wire.ObservationResult:
        if device is None:
            return wire.ObservationResult(observation_id=observation_id, error="unknown_device")
        try:
            data, screen = await capture(device, settle=settle)
            await self.upload(observation_id, data)
        except DeviceError as error:
            return wire.ObservationResult(observation_id=observation_id, error=error.reason[:64])
        except Exception:
            log.exception("screenshot upload failed")
            return wire.ObservationResult(observation_id=observation_id, error="upload_failed")
        screen = screen.model_copy(update={"sliders": await self._read_sliders(device, screen)})
        return wire.ObservationResult(
            observation_id=observation_id,
            captured_at=datetime.now(UTC),
            screen=screen,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    async def action(self, message: wire.Action) -> None:
        existing = self.journal.receive(message.action_id, message.device_id, message.digest)
        if existing is not None:
            # A repeated delivery: never execute twice. Repeat the result if we have one.
            if existing.digest != message.digest:
                log.error("action %s was redelivered with other parameters", message.action_id)
                return
            previous = self.journal.outbox_message(message.action_id)
            if previous:
                await self.send(previous)
            return
        async with self._lock(message.device_id):
            outcome, reason, extra = await self._execute(message)
            result = wire.ActionResult(
                action_id=message.action_id,
                outcome=outcome,
                reason=reason,
                finished_at=datetime.now(UTC),
                **extra,
            )
            self.journal.finish(message.action_id, outcome, reason, result.model_dump_json())
            await self.send(result.model_dump_json())
            if outcome != "rejected":
                self.journal.mark_seen(message.post_observation_id)
                device = self.by_id.get(message.device_id)
                settle = 0.0
                if device is not None and isinstance(message.input, Pinch):
                    settle = PINCH_SETTLE_MAX
                elif device is not None and (
                    (isinstance(message.input, Swipe) and device.swipe_coasts)
                    or not device.settles_on_its_own
                ):
                    settle = SETTLE_MAX
                post = await self._observe(device, message.post_observation_id, settle=settle)
                await self._emit(message.post_observation_id, post)

    def _check_current(self, message: wire.Action) -> None:
        """The action is of the device's current epoch, no stop came for it, and it is
        not past its deadline."""
        epoch, stopped = self.journal.epochs(message.device_id)
        if message.epoch != epoch:
            raise Rejected("stale_epoch")
        if message.epoch <= stopped:
            raise Rejected("stopped")
        if datetime.now(UTC) >= message.expires_at:
            raise Rejected("expired")

    async def _execute(self, message: wire.Action) -> tuple[str, str | None, dict[str, Any]]:
        device = self.by_id.get(message.device_id)
        try:
            if device is None:
                raise Rejected("unknown_device")
            self._check_current(message)
            command = message.input
            if command.kind not in device.capabilities:
                raise Rejected("unsupported_input")
            if isinstance(command, FindApp):
                found = apps.match(command.query, await device.list_apps())
                return (
                    "returned",
                    None,
                    {"apps": [wire.FoundApp(app_id=a, name=n) for a, n in found]},
                )
            if isinstance(command, TypeText):
                if command.method not in device.type_methods:
                    raise Rejected("unsupported_method")
                point = None
                if command.field_x is not None and command.field_y is not None:
                    point = to_input(command.field_x, command.field_y, message.expected)
                # Secrets, missing focus and the wrong field stop here.
                await device.check_text_target(point)
            opened: dict[str, Any] = {}  # by query: what matched, the opened one first
            if isinstance(command, LaunchApp):
                if command.query is not None:  # the best match, as find_app ranks them
                    matches = apps.match(command.query, await device.list_apps())
                    if not matches:
                        raise Rejected("app_not_found")
                    opened["apps"] = [wire.FoundApp(app_id=a, name=n) for a, n in matches]
                    command = LaunchApp(app_id=matches[0][0])
                assert command.app_id is not None
                await device.check_app(command.app_id)  # a wrong guess stops here
            state = await device.state()
            expected = message.expected
            if (
                not same_geometry(state, expected)
                or state.foreground_app != expected.foreground_app
            ):
                raise Rejected("stale_observation")
            if isinstance(command, SetPicker):
                await device.check_picker_target(*to_input(command.x, command.y, expected))
            if isinstance(command, SelectOption):
                to_input(command.x, command.y, expected)  # the point must be on the screenshot
            run = self._prepare(device, command, expected)
            # The checks above wait on the device: a stop, a new lease or the deadline may
            # have come meanwhile, and no input starts after them (``barrier``). Nothing is
            # awaited from here to the intent.
            self._check_current(message)
        except Rejected as error:
            return "rejected", error.reason, {}
        except WrongField as error:  # the agent is told which field has the focus
            return "rejected", error.reason, self._field_report(error.field, message.expected)
        except DeviceError as error:
            return "rejected", error.reason[:64], {}
        except Exception:
            # Nothing reached the device yet: a rejection, so the cloud is answered and
            # the device is not held by an action that never ends.
            log.exception("checking an input failed")
            return "rejected", "adapter_error", {}

        self.journal.intent(message.action_id)
        try:
            timeout = (
                SELECT_TIMEOUT
                if isinstance(command, SelectOption)
                else KEYS_TIMEOUT
                if isinstance(command, TypeText) and command.method != "set"
                else INPUT_TIMEOUT
            )
            async with asyncio.timeout(timeout):
                done = await run()
        except (DeviceError, TimeoutError) as error:
            # The input may have reached the device. Report it as such; never retry. (An
            # adapter that fails before it sends anything returns why instead of raising.)
            reason = error.reason if isinstance(error, DeviceError) else "timeout"
            return "unknown", reason[:64], {}
        except Exception:
            log.exception("input failed")
            return "unknown", "adapter_error", {}
        if isinstance(done, Selection):  # select_option: what was tapped, or why nothing was
            if done.selected is None:
                return "returned", done.problem, {}
            return "returned", None, {"field_text": done.selected[: wire.FIELD_TEXT_MAX]}
        # An input that stopped short says why: typing, or a picker. So does one that
        # failed while the adapter only read, before it sent anything: returned, not unknown.
        problem = done[:64] if isinstance(done, str) else None
        if device is not None and isinstance(command, TypeText):
            # What the field holds is read either way.
            read = await self._read_back(device, command.text)
            if read is None:
                return "returned", problem, {}
            return (
                "returned",
                problem,
                {"field_text": read.text} | self._field_report(read, message.expected),
            )
        return "returned", problem, opened

    @staticmethod
    def _field_report(read: TextField, screen: wire.ScreenState) -> dict[str, Any]:
        """A field's name and box for the cloud; the box in screenshot pixels, the space the
        agent's own coordinates are in."""
        box = None
        if read.box is not None:
            x, y, width, height = read.box
            sx, sy = screen.width / screen.input_width, screen.height / screen.input_height
            box = [x * sx, y * sy, width * sx, height * sy]
        return {"field_name": read.name, "field_box": box}

    @staticmethod
    async def _read_sliders(device: Device, screen: wire.ScreenState) -> list[wire.Slider]:
        """The sliders in view with the position each reports, centres in screenshot pixels.
        None when they cannot be read: the screenshot is worth more than this."""
        try:
            async with asyncio.timeout(SLIDERS_TIMEOUT):
                readings = await device.read_sliders()
        except (DeviceError, TimeoutError):
            return []
        except Exception:
            log.warning("reading the sliders failed", exc_info=True)
            return []
        sliders = []
        for reading in readings[: wire.MAX_SLIDERS]:
            x, y = reading.center
            sliders.append(
                wire.Slider(
                    name=reading.name,
                    value=reading.value,
                    x=x * screen.width / screen.input_width,
                    y=y * screen.height / screen.input_height,
                )
            )
        return sliders

    @staticmethod
    async def _read_back(device: Device, typed: str) -> TextField | None:
        """The focused field after typing, or None if it cannot be had. Never fails the
        input: the input is done either way."""
        try:
            async with asyncio.timeout(READ_BACK_TIMEOUT):
                read = await device.read_text_field(typed)
        except (DeviceError, TimeoutError):
            return None
        except Exception:
            log.warning("reading the field back failed")  # no traceback: it carries text
            return None
        return read if len(read.text) <= wire.FIELD_TEXT_MAX else None

    @staticmethod
    def _prepare(
        device: Device, command: object, screen: wire.ScreenState, *, person: bool = False
    ) -> Callable[[], Awaitable[Any]]:
        if isinstance(command, Tap):
            x, y = to_input(command.x, command.y, screen)
            return lambda: device.tap(x, y)
        if isinstance(command, Swipe):
            x1, y1 = to_input(command.x1, command.y1, screen)
            x2, y2 = to_input(command.x2, command.y2, screen)
            # Every point of a path is mapped, and must be on the screenshot, like the ends.
            path = (
                None
                if command.path is None
                else [(*to_input(p.x, p.y, screen), p.t_ms) for p in command.path]
            )
            return lambda: device.swipe(
                x1, y1, x2, y2, command.duration_ms, command.hold_ms, path=path
            )
        if isinstance(command, LongPress):
            x, y = to_input(command.x, command.y, screen)
            return lambda: device.long_press(x, y, command.duration_ms)
        if isinstance(command, DoubleTap):
            x, y = to_input(command.x, command.y, screen)
            return lambda: device.double_tap(x, y)
        if isinstance(command, Pinch):
            x, y = to_input(command.x, command.y, screen)
            middle, start, end = pinch_fingers(
                x, command.scale, screen.input_width, screen.input_height
            )
            return lambda: device.pinch(middle, y, start, end)
        if isinstance(command, TypeText):
            return lambda: device.type_text(
                command.text, replace=command.replace, method=command.method, person=person
            )
        if isinstance(command, SetPicker):
            x, y = to_input(command.x, command.y, screen)
            return lambda: device.set_picker(x, y, command.value)
        if isinstance(command, SelectOption):
            x, y = to_input(command.x, command.y, screen)
            return lambda: device.select_option(x, y, command.value)
        if isinstance(command, Back):
            return device.back
        if isinstance(command, Home):
            return device.home
        if isinstance(command, VolumeUp):
            return device.volume_up
        if isinstance(command, VolumeDown):
            return device.volume_down
        if isinstance(command, Lock):
            return device.lock
        if isinstance(command, LaunchApp):
            app_id = command.app_id
            assert app_id is not None  # a query is resolved before
            return lambda: device.launch_app(app_id)
        raise Rejected("unsupported_input")

    # ------------------------------------------------------------ takeover

    async def frame(self, message: wire.FrameRequest) -> None:
        """One live picture for a person who took over the device or watches its task.
        Nothing is stored. A phone a person takes over that is locked or dark is woken
        for them first (watching never touches the phone)."""
        device = self.by_id.get(message.device_id)
        try:
            if device is None:
                raise DeviceError("unknown_device")
            try:
                frame = await self._picture(device, message)
            except DeviceError as error:
                if not (error.reason == "locked" and message.stream):
                    raise
                if not await self._wake(device, message.device_id):
                    raise
                frame = await self._picture(device, message)
        except DeviceError as error:
            frame = wire.Frame(request_id=message.request_id, error=error.reason[:64])
        await self.send(frame.model_dump_json())

    async def _picture(self, device: Device, message: wire.FrameRequest) -> wire.Frame:
        frame = await self._broadcast(device, message) if message.stream else None
        if frame is None:
            if message.if_idle:
                data, screen = await self._glance(device, message.device_id)
            else:
                async with self._lock(message.device_id):
                    data, screen = await capture(device)
            jpeg = base64.b64encode(to_jpeg(data, max_side=message.max_side)).decode()
            frame = wire.Frame(request_id=message.request_id, screen=screen, jpeg=jpeg)
        return frame

    async def _wake(self, device: Device, device_id: UUID) -> bool:
        """Wakes a locked or dark phone for a person taking it over, at most once every
        WAKE_BACKOFF seconds; whether it was tried. Its known geometry is dropped: the
        screen may come back turned."""
        loop = asyncio.get_running_loop()
        if loop.time() - self._woken_at.get(device_id, -WAKE_BACKOFF) < WAKE_BACKOFF:
            return False
        self._woken_at[device_id] = loop.time()
        self._states.pop(device_id, None)
        log.info("waking a locked phone for a takeover", extra={"device": device.native_id})
        async with self._lock(device_id):
            with contextlib.suppress(DeviceError, TimeoutError):
                async with asyncio.timeout(WAKE_TIMEOUT):
                    await device.wake()
        return True

    async def _glance(self, device: Device, device_id: UUID) -> tuple[bytes, wire.ScreenState]:
        """A screenshot for a person watching a task. Watching gives way to the agent: it is
        skipped while an input or screenshot is under way (``busy``), never opens a driver
        session (``session_lost``: keeping the phone warm, or the agent's next command, opens
        it; opening can take minutes), and lets go of the device after LIVE_FRAME_TIMEOUT
        (``timeout``, before the cloud stops waiting), so a phone that hangs holds up the
        agent's next input for that long at most, not for the driver's own 30 s per command.
        Cancelling the hung command closes its connection; the adapter keeps its session."""
        lock = self._lock(device_id)
        if lock.locked():
            raise DeviceError("busy")  # an input or screenshot is under way
        if not device.session_open:
            raise DeviceError("session_lost")

        async def locked() -> tuple[bytes, wire.ScreenState]:
            async with lock:
                return await capture(device)

        try:
            return await asyncio.wait_for(locked(), wire.LIVE_FRAME_TIMEOUT)
        except TimeoutError:
            raise DeviceError("timeout") from None

    async def _broadcast(self, device: Device, message: wire.FrameRequest) -> wire.Frame | None:
        """The next picture of the phone's own screen broadcast, or None when it has none
        to show (the caller takes a screenshot). It does not wait for an input to finish:
        a person sees the phone move while it is being swiped."""
        async with self._streaming.setdefault(message.device_id, asyncio.Lock()):
            picture = await device.stream_frame(
                self._streamed.get(message.device_id, 0), STREAM_WAIT
            )
            if picture is None:
                return None
            self._streamed[message.device_id] = picture.number
        # Decoding and encoding a picture is work for a thread, not for the loop that also
        # keeps the heartbeat and the other phones going.
        jpeg, width, height = await asyncio.to_thread(fit_jpeg, picture.jpeg, message.max_side)
        for fresh in (False, True):
            state = await self._broadcast_state(device, message.device_id, fresh=fresh)
            if state is None:
                return None  # the phone's geometry could not be read now: a screenshot instead
            # The picture has the shape of the screen, or the phone was turned since the
            # geometry was read: read it again once, then give up on this picture.
            if abs(width * state.input_height - height * state.input_width) <= max(width, height):
                screen = wire.ScreenState(
                    width=width,
                    height=height,
                    input_width=state.input_width,
                    input_height=state.input_height,
                    rotation=state.rotation,
                    foreground_app=state.foreground_app,
                )
                encoded = base64.b64encode(jpeg).decode()
                return wire.Frame(request_id=message.request_id, screen=screen, jpeg=encoded)
        return None

    async def _broadcast_state(
        self, device: Device, device_id: UUID, *, fresh: bool
    ) -> DeviceState | None:
        """The phone's geometry for a broadcast picture, or None when a fresh one was needed
        and could not be had in time. A known one is returned at once, however old: it is
        refreshed in the background (``_refresh_state``)."""
        loop = asyncio.get_running_loop()
        known = self._states.get(device_id)
        if known is None and not fresh:
            primed = getattr(device, "last_state", None)  # read while it was being kept ready
            if primed is not None:
                known = self._states[device_id] = (loop.time(), primed)
        if known is not None and not fresh:
            if loop.time() - known[0] >= STREAM_STATE_TTL:
                self._refresh_soon(device, device_id)
            return known[1]
        try:
            return await asyncio.wait_for(
                self._read_state(device, device_id), STREAM_STATE_TIMEOUT if fresh else None
            )
        except (TimeoutError, DeviceError):
            if known is None:
                raise  # the first picture of a phone cannot do without it
            self._state_failed_at[device_id] = loop.time()
            return None

    def _refresh_soon(self, device: Device, device_id: UUID) -> None:
        """Starts one read of the geometry in the background, unless one is under way, an
        input holds the phone, or the last one failed a moment ago."""
        loop = asyncio.get_running_loop()
        running = self._refreshing.get(device_id)
        if running is not None and not running.done():
            return
        if loop.time() - self._state_failed_at.get(device_id, -STREAM_STATE_BACKOFF) < (
            STREAM_STATE_BACKOFF
        ):
            return
        if self._lock(device_id).locked():
            return  # an input is under way: do not queue behind it for this

        async def refresh() -> None:
            try:
                await asyncio.wait_for(self._read_state(device, device_id), STREAM_STATE_TIMEOUT)
            except (TimeoutError, DeviceError):
                self._state_failed_at[device_id] = loop.time()

        self._refreshing[device_id] = loop.create_task(refresh())

    async def _read_state(self, device: Device, device_id: UUID) -> DeviceState:
        async with self._lock(device_id):
            state = await device.state()
        self._states[device_id] = (asyncio.get_running_loop().time(), state)
        return state

    async def manual(self, message: wire.ManualInput) -> None:
        """A person's input. It runs at most once, only under the takeover's epoch, and
        without the secure-field check: typing a password is what the person is for.
        The text is never logged."""
        if not self.journal.mark_seen(message.input_id):
            return  # a repeated delivery never repeats input
        async with self._lock(message.device_id):
            outcome, reason = await self._manual(message)
        result = wire.ManualInputResult(input_id=message.input_id, outcome=outcome, reason=reason)
        await self.send(result.model_dump_json())

    async def _manual(self, message: wire.ManualInput) -> tuple[str, str | None]:
        device = self.by_id.get(message.device_id)
        command = message.input
        try:
            if device is None:
                raise Rejected("unknown_device")
            epoch, stopped = self.journal.epochs(message.device_id)
            if message.epoch != epoch:
                raise Rejected("stale_epoch")
            if message.epoch <= stopped:
                raise Rejected("stopped")
            if not isinstance(command, MANUAL_INPUTS) or command.kind not in device.capabilities:
                raise Rejected("unsupported_input")
            # The frame may be older than a turn of the phone. Another app is fine: the
            # person is moving between apps.
            if not same_geometry(await device.state(), message.screen):
                raise Rejected("screen_changed")
            run = self._prepare(device, command, message.screen, person=True)
        except (Rejected, DeviceError) as error:  # nothing touched the device yet
            return "rejected", error.reason[:64]
        try:
            async with asyncio.timeout(INPUT_TIMEOUT):
                done = await run()
        except (DeviceError, TimeoutError) as error:
            return "unknown", (error.reason if isinstance(error, DeviceError) else "timeout")[:64]
        except Exception:
            log.error("manual input failed")  # no traceback: it could carry typed text
            return "unknown", "adapter_error"
        if isinstance(command, Lock):
            # The person locked it: the next picture must not wake it again. It finds the
            # phone locked, and the takeover ends (``device_locked``).
            self._woken_at[message.device_id] = asyncio.get_running_loop().time()
            self._states.pop(message.device_id, None)
        # Typing that stopped short, or failed before it sent anything, says why, as for
        # the agent.
        return "returned", done[:64] if isinstance(done, str) else None

    def barrier(self, message: wire.Stop) -> None:
        """Puts the stop barrier on disk. Called as soon as the message arrives, so no
        input of that epoch that is still queued can start afterwards."""
        self.journal.stop(message.device_id, message.epoch)

    async def confirm_stop(self, message: wire.Stop) -> None:
        async with self._lock(message.device_id):
            pass  # waits for an input that is already in progress
        ack = wire.StopAck(stop_id=message.stop_id, device_id=message.device_id)
        await self._emit(message.stop_id, ack)

    async def stop(self, message: wire.Stop) -> None:
        self.barrier(message)
        await self.confirm_stop(message)
