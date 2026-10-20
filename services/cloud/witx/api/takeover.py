"""The browser side of a takeover : one WebSocket per takeover.

The browser authenticates in its first message, then receives frames (a JPEG every
FRAME_INTERVAL or so) and sends input. Each input names the frame its coordinates
refer to; the Gateway maps them with that frame's geometry and runs them under the
takeover's epoch only. Typed text goes to the device and nowhere else: it is not
logged, stored or put into an event.

The takeover ends when the person ends it, after rules.TAKEOVER_IDLE without input
(or a keep-alive, sent when the person says they are still there),
when the socket drops, when the phone sends no picture for FIRST_FRAME_TIMEOUT, when its
screen stays locked after the Gateway tried waking it (``device_locked``), or when the
takeover was ended elsewhere (the end endpoint, stopping the task).
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError
from witx_contracts import api
from witx_contracts import gateway as wire

from witx.domain import rules
from witx.domain.rules import Rejected
from witx.hub import Hub
from witx.services import takeover
from witx.services.identity import Actor
from witx.store import devices, tasks
from witx.store.db import Database

log = logging.getLogger(__name__)

# Seconds from one frame request to the next, at most 30 per second. A phone with a screen
# broadcast answers as the pictures come; one without is limited by taking screenshots.
FRAME_INTERVAL = 1 / 30
# Frame requests in flight at once. With one, every picture waits for the trip to the
# Gateway and back before the next is asked for, so a Gateway far away caps the rate at
# 1 / round trip; with two, the next picture is already being waited for at the Gateway
# (which answers them one at a time, in order) while this one travels to the browser.
FRAME_WINDOW = 2
FRAME_RETRY = 0.5  # after a request that got no picture (a locked phone, say)
FRAME_MAX_SIDE = 1280  # the JPEG's longer side; taps map through the screen geometry
LIVE_MAX_SIDE = 900  # a watched screen is shown small, in the task's side panel
# A little longer than the Gateway spends on the picture, so its answer (``timeout``) arrives.
LIVE_TIMEOUT = wire.LIVE_FRAME_TIMEOUT + 1.0
FRAME_TIMEOUT = 10.0
# The first picture of a takeover may wait for the phone's session to open: tens of seconds
# on a real iPhone (WebDriverAgent is started for it). A takeover that gets no picture for
# this long, at the start or later, ends (``device_not_responding``): the phone stopped
# answering, and the person would otherwise look at "connecting" until the idle time ran out.
FIRST_FRAME_TIMEOUT = 45.0
INPUT_TIMEOUT = 40.0
# Input may refer to any frame sent within this long: a gesture names the frame on the
# canvas when the finger was let go, so the longest drag (5 s) plus its hold (15 s) plus the
# way to the browser and back must fit. Kept by time, not by count: frames come up to 30 a
# second, and a count of them was outlived by a slow drag.
KEPT_FRAMES_S = 30.0

Receive = Callable[[], Awaitable[str]]
Send = Callable[[str], Awaitable[None]]


class Ended(Exception):
    def __init__(self, reason: str, resume: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.resume = resume


async def serve(
    db: Database, hub: Hub, *, takeover_id: UUID, who: Actor, receive: Receive, send: Send
) -> None:
    """Runs one takeover connection until the takeover ends."""
    async with db.tenant(who.tenant_id) as conn:
        lease = await devices.lease_by_takeover(conn, takeover_id)
        device = await devices.get(conn, lease.device_id) if lease else None
    now = datetime.now(UTC)
    if (
        lease is None
        or device is None
        or lease.takeover_user != who.user_id
        or not rules.takeover_active(lease, now)
    ):
        await send(api.TakeoverEnded(reason="not_active").model_dump_json())
        return
    # The geometry of each frame sent, by frame_id, with the time it was sent; input is mapped
    # with the geometry of the frame it names.
    screens: dict[int, tuple[float, wire.ScreenState]] = {}
    inputs = 0
    outcome = Ended("disconnected")

    async def frames() -> None:
        frame_id = 0
        loop = asyncio.get_running_loop()
        in_flight: deque[tuple[float, asyncio.Task[wire.GatewayMessage]]] = deque()

        def ask() -> None:
            request = wire.FrameRequest(
                request_id=uuid4(), device_id=device.id, max_side=FRAME_MAX_SIDE, stream=True
            )
            in_flight.append(
                (
                    loop.time(),
                    asyncio.create_task(
                        hub.ask(
                            device.gateway_id,
                            request,
                            FIRST_FRAME_TIMEOUT if frame_id == 0 else FRAME_TIMEOUT,
                        )
                    ),
                )
            )

        last_picture = loop.time()
        try:
            while True:
                while len(in_flight) < FRAME_WINDOW:
                    ask()
                started, pending = in_flight.popleft()
                try:
                    frame: wire.GatewayMessage | None = await pending
                except Rejected as error:
                    if error.code != "device_timeout":
                        raise  # the Gateway went away: device_offline
                    frame = None  # no answer in time: no picture, like an answer with an error
                interval = FRAME_RETRY
                if isinstance(frame, wire.Frame) and frame.screen and frame.jpeg:
                    last_picture = loop.time()
                    interval = FRAME_INTERVAL
                    frame_id += 1
                    screens[frame_id] = (loop.time(), frame.screen)
                    forget_before = loop.time() - KEPT_FRAMES_S
                    for old, (sent_at, _) in list(screens.items()):
                        if sent_at >= forget_before:
                            break  # the rest came later
                        del screens[old]
                    await send(
                        api.TakeoverFrame(
                            frame_id=frame_id,
                            width=frame.screen.width,
                            height=frame.screen.height,
                            rotation=frame.screen.rotation,
                            jpeg=frame.jpeg,
                        ).model_dump_json()
                    )
                elif isinstance(frame, wire.Frame) and frame.error == "locked":
                    # The Gateway tried waking it: the screen stays locked (a passcode).
                    raise Ended("device_locked")
                elif loop.time() - last_picture >= FIRST_FRAME_TIMEOUT:
                    raise Ended("device_not_responding")
                # Capturing usually takes longer than the interval: then the next one starts at
                # once. After a request that got no picture, the ones already asked for wait too.
                await asyncio.sleep(max(0.0, interval - (loop.time() - started)))
        finally:
            for _, pending in in_flight:
                pending.cancel()
                # A request that failed meanwhile (the Gateway went away) is not news.
                pending.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def still_ours() -> int:
        """The takeover's epoch, as long as it is still running."""
        async with db.tenant(who.tenant_id) as conn:
            current = await devices.lease_by_takeover(conn, takeover_id)
        if current is None:
            raise Ended("ended_elsewhere")
        if not rules.takeover_active(current, datetime.now(UTC)):
            raise Ended("timeout")
        return current.epoch

    async def handle(raw: str, last_seq: int) -> int:
        """Handles one browser message; returns the new last input sequence."""
        try:
            finish = api.TakeoverFinish.model_validate_json(raw)
        except ValidationError:
            finish = None
        if finish is not None:
            raise Ended("ended", finish.resume)
        try:
            api.TakeoverKeepAlive.model_validate_json(raw)
        except ValidationError:
            pass
        else:
            await still_ours()
            async with db.tenant(who.tenant_id) as conn:
                await takeover.extend(conn, takeover_id, datetime.now(UTC))
            return last_seq
        message = api.TakeoverInput.model_validate_json(raw)  # anything else closes
        epoch = await still_ours()
        in_order = message.client_seq > last_seq  # an older input arriving late is dropped
        last_seq = max(last_seq, message.client_seq)
        refusal = (
            "out_of_order"
            if not in_order
            else "stale_frame"
            if message.frame_id not in screens
            else "unsupported_input"
            if message.input.get("kind") not in rules.MANUAL_INPUT_KINDS
            else None
        )
        if refusal is not None:
            result = api.TakeoverInputResult(
                input_id=message.input_id, outcome="rejected", reason=refusal
            )
        else:
            result = await run(message, epoch, screens[message.frame_id][1])
        await send(result.model_dump_json())
        return last_seq

    async def run(
        message: api.TakeoverInput, epoch: int, frame: wire.ScreenState
    ) -> api.TakeoverInputResult:
        """Checks one input and has the Gateway carry it out. The input itself is checked
        here, so a malformed one (a drag's path out of order, say) costs one refusal, not
        the takeover; its points must be on the frame, the same rule as for the agent's."""
        nonlocal inputs
        try:
            command = wire.action_input.validate_python(message.input)
            rules.check_points(message.input, frame.width, frame.height)
        except ValidationError:
            return api.TakeoverInputResult(
                input_id=message.input_id, outcome="rejected", reason="invalid_input"
            )
        except Rejected as error:
            return api.TakeoverInputResult(
                input_id=message.input_id, outcome="rejected", reason=error.code
            )
        manual = wire.ManualInput(
            input_id=message.input_id,
            device_id=device.id,
            epoch=epoch,
            input=command,
            screen=frame,
        )
        answer = await hub.ask(device.gateway_id, manual, INPUT_TIMEOUT)
        assert isinstance(answer, wire.ManualInputResult)
        inputs += answer.outcome != "rejected"
        async with db.tenant(who.tenant_id) as conn:
            await takeover.extend(conn, takeover_id, datetime.now(UTC))
            if lease.task_id is not None:
                # The kind only: typed text never goes into an event.
                await tasks.emit(
                    conn,
                    lease.task_id,
                    "takeover.input",
                    kind=manual.input.kind,
                    outcome=answer.outcome,
                )
        return api.TakeoverInputResult(
            input_id=message.input_id, outcome=answer.outcome, reason=answer.reason
        )

    async def next_message() -> str:
        return await receive()

    pump = asyncio.create_task(frames())
    try:
        last_seq = 0
        while True:
            incoming = asyncio.create_task(next_message())
            done, _ = await asyncio.wait(
                {incoming, pump},
                timeout=rules.TAKEOVER_IDLE.total_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pump in done:
                incoming.cancel()
                pump.result()  # raises why the frames stopped (device offline, ...)
            if incoming not in done:
                incoming.cancel()
                raise Ended("timeout")
            last_seq = await handle(incoming.result(), last_seq)
    except Ended as ended:
        outcome = ended
    except Rejected as error:
        outcome = Ended(error.code)
    except ValidationError:
        outcome = Ended("invalid_message")
    except Exception:  # the socket closed or failed
        outcome = Ended("disconnected")
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump
        async with db.tenant(who.tenant_id) as conn:
            await takeover.end(
                conn,
                takeover_id=takeover_id,
                reason=outcome.reason,
                inputs=inputs,
                resume=outcome.resume,
            )
        with contextlib.suppress(Exception):
            await send(api.TakeoverEnded(reason=outcome.reason).model_dump_json())


async def live_frame(
    db: Database, hub: Hub, *, who: Actor, task_id: UUID, device_id: UUID
) -> api.LiveFrame:
    """The screen of a device the task holds, for a person watching the task. It is not
    stored, and it is skipped (``device_busy``) while the device carries out an input or
    takes the agent's screenshot, and the Gateway gives up on it after LIVE_FRAME_TIMEOUT, so
    watching never holds the device for long while the agent waits; and during a
    takeover, which has its own picture. A device that is not usable (offline, not
    responding) is not asked at all."""
    async with db.tenant(who.tenant_id) as conn:
        lease = await devices.lease(conn, device_id)
        device = await devices.get(conn, device_id)
    if lease is None or lease.task_id != task_id or device is None:
        raise Rejected("not_found", "the task does not hold this device")
    if lease.takeover_id is not None:
        raise Rejected("device_busy")
    rules.require_usable(device)
    request = wire.FrameRequest(
        request_id=uuid4(), device_id=device_id, max_side=LIVE_MAX_SIDE, if_idle=True
    )
    frame = await hub.ask(device.gateway_id, request, LIVE_TIMEOUT)
    if not isinstance(frame, wire.Frame) or frame.screen is None or frame.jpeg is None:
        reason = frame.error if isinstance(frame, wire.Frame) else None
        raise Rejected("device_busy" if reason == "busy" else "frame_unavailable", reason or "")
    return api.LiveFrame(
        width=frame.screen.width,
        height=frame.screen.height,
        jpeg=frame.jpeg,
        captured_at=datetime.now(UTC),
    )
