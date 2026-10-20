"""WebSocket protocol between a Gateway and the cloud Hub (protocol version 1).

Every message is one JSON object with a ``type`` field. Screenshots never travel
over the socket: the Gateway uploads them over HTTP and then reports metadata here.

Delivery is at-least-once in both directions. Receivers deduplicate by the IDs in
the messages (``observation_id``, ``action_id``, ``stop_id``); a repeated message
never repeats device input.

A person taking over a device  uses a live request/response pair on the
same socket: ``FrameRequest``/``Frame`` for the live picture (a JPEG inline, never
stored) and ``ManualInput``/``ManualInputResult`` for their input. These are not
resent after a reconnect; the takeover simply ends.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from witx_contracts.common import ActionInput, Capability, Platform, Problem, Rotation, Wire

PROTOCOL_VERSION = 1

NativeId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Epoch = Annotated[int, Field(ge=1)]


# ---------------------------------------------------------------- shared payloads


class DeviceInfo(Wire):
    native_id: NativeId
    platform: Platform
    name: str = Field(min_length=1, max_length=128)
    # Apps in which every input counts as sensitive (payments, banking, messaging).
    sensitive_apps: list[str] = Field(default_factory=list, max_length=64)
    capabilities: list[Capability]
    # Empty when the device can take tasks (device pool, #33).
    problems: list[Problem] = Field(default_factory=list, max_length=16)


MAX_SLIDERS = 8


class Slider(Wire):
    """A slider on the screen and the position it reports: what the phone itself says,
    "27%" on iOS, the step number ("1.0") on Android. Where it is, as the centre in screenshot
    pixels, ties it to the slider on the picture (some have no name)."""

    name: str = Field(max_length=200)
    value: str = Field(max_length=32)
    x: float
    y: float


class ScreenState(Wire):
    """What the device reported when the screenshot was taken.

    ``width``/``height`` are screenshot pixels; ``input_width``/``input_height`` are the
    coordinate space input is sent in (Android surface pixels, iOS window points).
    Both describe the current orientation, so mapping is a pure scale. ``rotation`` is
    the screen's turn clockwise from the device's natural orientation, in degrees: a
    phone turned the other way round has the same size but another rotation.
    """

    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    input_width: int = Field(ge=1, le=16384)
    input_height: int = Field(ge=1, le=16384)
    rotation: Rotation
    foreground_app: str = Field(min_length=1, max_length=255)
    # Only on a screenshot the agent gets (not on live pictures): the sliders in view.
    sliders: list[Slider] = Field(default_factory=list, max_length=MAX_SLIDERS)


# ---------------------------------------------------------------- Gateway -> cloud


class Hello(Wire):
    type: Literal["hello"] = "hello"
    protocol: Literal[1] = 1
    gateway_version: str = Field(max_length=64)
    devices: list[DeviceInfo] = Field(max_length=64)


class ObservationResult(Wire):
    """Sent after the screenshot was uploaded. ``error`` is set when capture failed."""

    type: Literal["observation_result"] = "observation_result"
    observation_id: UUID
    captured_at: datetime | None = None
    screen: ScreenState | None = None
    sha256: Sha256 | None = None
    error: str | None = Field(default=None, max_length=64)


class FoundApp(Wire):
    app_id: str = Field(max_length=255)
    name: str | None = Field(default=None, max_length=128)


FIELD_TEXT_MAX = 2000


class ActionResult(Wire):
    """Dispatch outcome. It says what the Gateway did, not whether the goal was achieved.

    - ``rejected``: input was never sent to the device (``reason`` explains why).
    - ``returned``: the adapter call returned; the business effect is still unverified.
    - ``unknown``: input may or may not have reached the device.
    """

    type: Literal["action_result"] = "action_result"
    action_id: UUID
    outcome: Literal["rejected", "returned", "unknown"]
    reason: str | None = Field(default=None, max_length=64)
    finished_at: datetime
    # find_app only: the installed apps that match, best first (empty: none matches).
    apps: list[FoundApp] | None = Field(default=None, max_length=5)
    # type_text: what the focused field holds once the text is in, so a lost
    # or changed character is caught at once. None when it cannot be read: no field, a
    # password field, or more text than fits here.
    field_text: str | None = Field(default=None, max_length=FIELD_TEXT_MAX)
    # ... and which field that was: what the app calls it, and where it is (x, y, width,
    # height in input points), so a text that went into another field than the agent
    # meant can be told.
    field_name: str | None = Field(default=None, max_length=200)
    field_box: list[float] | None = Field(default=None, min_length=4, max_length=4)


class StopAck(Wire):
    """The stop barrier is in place and no input for this epoch is in flight."""

    type: Literal["stop_ack"] = "stop_ack"
    stop_id: UUID
    device_id: UUID


class Devices(Wire):
    """The Gateway's devices again, after one was plugged in, unplugged or changed state."""

    type: Literal["devices"] = "devices"
    devices: list[DeviceInfo] = Field(max_length=64)


class Heartbeat(Wire):
    type: Literal["heartbeat"] = "heartbeat"


class Frame(Wire):
    """One picture of the screen for a takeover, as a base64 JPEG. Not stored."""

    type: Literal["frame"] = "frame"
    request_id: UUID
    screen: ScreenState | None = None
    jpeg: str | None = Field(default=None, max_length=8_000_000)
    error: str | None = Field(default=None, max_length=64)


class ManualInputResult(Wire):
    """What the Gateway did with a person's input (same outcomes as ``ActionResult``)."""

    type: Literal["manual_input_result"] = "manual_input_result"
    input_id: UUID
    outcome: Literal["rejected", "returned", "unknown"]
    reason: str | None = Field(default=None, max_length=64)


GatewayMessage = Annotated[
    Hello
    | Devices
    | ObservationResult
    | ActionResult
    | StopAck
    | Heartbeat
    | Frame
    | ManualInputResult,
    Field(discriminator="type"),
]
gateway_message: TypeAdapter[GatewayMessage] = TypeAdapter(GatewayMessage)


# ---------------------------------------------------------------- cloud -> Gateway


class DeviceBinding(Wire):
    native_id: NativeId
    device_id: UUID


class Welcome(Wire):
    type: Literal["welcome"] = "welcome"
    gateway_id: UUID
    devices: list[DeviceBinding]
    # The credential is due for rotation: ask for the next one (POST /gateway/v1/credential).
    rotate_credential: bool = False


class Bindings(Wire):
    """Device IDs for devices the Gateway reported after the welcome (plugged in later)."""

    type: Literal["bindings"] = "bindings"
    devices: list[DeviceBinding]


class Lease(Wire):
    """The current writer epoch of a device. Commands with another epoch are rejected."""

    type: Literal["lease"] = "lease"
    device_id: UUID
    epoch: Epoch


class Observe(Wire):
    type: Literal["observe"] = "observe"
    observation_id: UUID
    device_id: UUID


class Action(Wire):
    """One device input.

    ``expected`` is the screen state of the observation the coordinates refer to; the
    Gateway rejects the action if the device no longer matches it. ``digest`` binds
    the action ID to its parameters: the same ID with another digest is a conflict.
    """

    type: Literal["action"] = "action"
    action_id: UUID
    device_id: UUID
    epoch: Epoch
    input: ActionInput
    expected: ScreenState
    digest: Sha256
    expires_at: datetime
    post_observation_id: UUID


class Stop(Wire):
    type: Literal["stop"] = "stop"
    stop_id: UUID
    device_id: UUID
    epoch: Epoch


# Seconds a picture for a person watching a task (``if_idle``) may take: the Gateway gives
# up on it after this long, letting go of the device, so a phone that hangs is never held
# for watching while the agent waits; the cloud waits a little longer, for that answer.
LIVE_FRAME_TIMEOUT = 5.0


class FrameRequest(Wire):
    """A live picture of a device: for a takeover, or for a person watching a task.
    ``max_side`` scales the JPEG down (the reported screen geometry is unchanged);
    ``if_idle`` answers ``busy`` at once instead of waiting while the device works, takes
    at most LIVE_FRAME_TIMEOUT and never opens a driver session (``session_lost``);
    ``stream`` takes the picture from the phone's own screen broadcast when it has one,
    and waits for a newer one than the last it sent (a takeover asks for pictures in a
    row), and then reports the picture's own size as the screen."""

    type: Literal["frame_request"] = "frame_request"
    request_id: UUID
    device_id: UUID
    max_side: int | None = Field(default=None, ge=200, le=4000)
    if_idle: bool = False
    stream: bool = False


class ManualInput(Wire):
    """A person's input during a takeover. Coordinates are pixels of the frame ``screen``
    was reported with. Only the takeover's epoch is accepted, and no secure-field check
    applies: the person may type a password; the text is never logged or stored."""

    type: Literal["manual_input"] = "manual_input"
    input_id: UUID
    device_id: UUID
    epoch: Epoch
    input: ActionInput
    screen: ScreenState


class ResultAck(Wire):
    """The cloud stored a result; the Gateway may stop resending it."""

    type: Literal["result_ack"] = "result_ack"
    ref_id: UUID


action_input: TypeAdapter[ActionInput] = TypeAdapter(ActionInput)

CloudMessage = Annotated[
    Welcome | Bindings | Lease | Observe | Action | Stop | ResultAck | FrameRequest | ManualInput,
    Field(discriminator="type"),
]
cloud_message: TypeAdapter[CloudMessage] = TypeAdapter(CloudMessage)


# ---------------------------------------------------------------- machine identity (HTTP)


class Enrollment(Wire):
    """POST /gateway/v1/enroll: spends a one-time enrollment code."""

    code: str = Field(min_length=1, max_length=256)
    host: str = Field(min_length=1, max_length=255)


class Credential(Wire):
    """A Gateway's long-term credential, returned once (enrollment or rotation). After a
    rotation the previous credential works until this one is first used."""

    gateway_id: UUID
    credential: str
