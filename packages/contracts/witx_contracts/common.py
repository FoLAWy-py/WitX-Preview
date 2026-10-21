"""Types shared by the Web API and the Gateway protocol."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class Wire(BaseModel):
    """Base for every wire model: unknown fields are rejected and values are immutable.
    Every moment is written in UTC, however it was held: a database returns them in its
    session's zone, a stored result in UTC, and a client that compared them as written
    put a task's result before its first message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_serializer("*", when_used="json-unless-none")
    def _in_utc(self, value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(UTC)
        return value


Platform = Literal["android", "ios", "fake"]
# A screen's turn clockwise from the device's natural orientation, in degrees.
Rotation = Literal[0, 90, 180, 270]
# Why a device cannot take tasks now. The Gateway checks; the Web app says how to fix each.
Problem = Literal[
    "disconnected",  # known to the Gateway, but not connected now
    "tool_missing",  # adb or Xcode's command-line tools are not installed on the Gateway
    "adb_unauthorized",  # Android: USB debugging not yet allowed on the phone
    "adb_offline",  # Android: connected but not responding
    "not_paired",  # iPhone: "Trust This Computer" not accepted yet
    "developer_mode_off",  # iPhone: Developer Mode is off
    "appium_unreachable",  # the Appium server does not answer
    "driver_missing",  # Appium lacks the driver for this platform
    "wda_unsigned",  # real iPhone without WebDriverAgent signing configured
    "identity_changed",  # another device answered than the one first seen (forget-device)
    "not_responding",  # connected, but its agent could not be started or kept (keep_warm)
    "locked",  # its screen is locked or dark: a person unlocks it (a takeover tries waking it)
]

# Coordinates are pixels in the screenshot the model was shown.
Coordinate = Annotated[float, Field(ge=0, le=16384)]
# How text goes into a field: set into it, tapped on the keyboard, or pasted.
TypeMethod = Literal["set", "keys", "paste"]


class Tap(Wire):
    kind: Literal["tap"] = "tap"
    x: Coordinate
    y: Coordinate


class PathPoint(Wire):
    """Where the finger was ``t_ms`` after it pressed, in screenshot pixels."""

    x: Coordinate
    y: Coordinate
    t_ms: int = Field(ge=0, le=5000)


# A swipe's path has at most this many points (a drag sampled every 16 ms for 3 s).
PATH_MAX = 200


class Swipe(Wire):
    """Presses at (x1, y1), moves to (x2, y2) evenly in ``duration_ms``, stays pressed there
    for ``hold_ms`` and lets go. A control that acts while it is held (a virtual joystick)
    is pushed with a short move and a long hold.

    With ``path`` the finger follows that path instead of the straight line: a person's
    drag, as the browser sampled it. The path begins at (x1, y1) at 0 ms and ends at
    (x2, y2) at ``duration_ms``, in time order. The agent's swipe has no path."""

    kind: Literal["swipe"] = "swipe"
    x1: Coordinate
    y1: Coordinate
    x2: Coordinate
    y2: Coordinate
    duration_ms: int = Field(ge=50, le=5000)
    hold_ms: int = Field(default=0, ge=0, le=15000)
    path: list[PathPoint] | None = Field(default=None, min_length=2, max_length=PATH_MAX)

    @model_validator(mode="after")
    def _path_runs_from_start_to_end(self) -> "Swipe":
        if self.path is None:
            return self
        first, last = self.path[0], self.path[-1]
        if (first.x, first.y, first.t_ms) != (self.x1, self.y1, 0):
            raise ValueError("path must begin at (x1, y1) at 0 ms")
        if (last.x, last.y, last.t_ms) != (self.x2, self.y2, self.duration_ms):
            raise ValueError("path must end at (x2, y2) at duration_ms")
        if any(a.t_ms > b.t_ms for a, b in zip(self.path, self.path[1:], strict=False)):
            raise ValueError("path points must be in time order")
        return self


class LongPress(Wire):
    """Presses one point for ``duration_ms`` without moving, then lets go."""

    kind: Literal["long_press"] = "long_press"
    x: Coordinate
    y: Coordinate
    duration_ms: int = Field(ge=500, le=15000)


class DoubleTap(Wire):
    kind: Literal["double_tap"] = "double_tap"
    x: Coordinate
    y: Coordinate


class Pinch(Wire):
    """Two fingers on a level line through (x, y) move apart (``scale`` above 1: zoom in)
    or together (below 1: zoom out), so that what lies under them grows or shrinks about
    ``scale`` times."""

    kind: Literal["pinch"] = "pinch"
    x: Coordinate
    y: Coordinate
    scale: float = Field(ge=0.2, le=5)

    @model_validator(mode="after")
    def _changes_the_size(self) -> "Pinch":
        if self.scale == 1:
            raise ValueError("scale 1 changes nothing")
        return self


class TypeText(Wire):
    """Types into the focused field. The agent also says where that field is (a point
    inside it, as on the screenshot) and the Gateway refuses to type when the focused
    field is somewhere else; a person taking over names no field. With ``replace``
    the field is cleared first, so the text replaces what it held; without, the
    text goes in at the cursor. ``method`` is how it goes in: set into the field,
    tapped on the on-screen keyboard (for a box that closes on any other way), or pasted
    through the field's own menu."""

    kind: Literal["type_text"] = "type_text"
    text: str = Field(min_length=1, max_length=2000)
    field_x: Coordinate | None = None
    field_y: Coordinate | None = None
    replace: bool = False
    method: TypeMethod = "set"

    @model_validator(mode="after")
    def _both_or_neither(self) -> "TypeText":
        if (self.field_x is None) != (self.field_y is None):
            raise ValueError("field_x and field_y go together")
        return self


class SetPicker(Wire):
    """Turns the picker wheel under (x, y) to an item, named as the wheel displays it."""

    kind: Literal["set_picker"] = "set_picker"
    x: Coordinate
    y: Coordinate
    value: str = Field(min_length=1, max_length=200)


class SelectOption(Wire):
    """Scrolls the list around (x, y) until an option showing this text is on the screen,
    then taps it. The Gateway finds it by its text, not by a coordinate, and taps
    nothing when no option, or more than one, shows exactly this text."""

    kind: Literal["select_option"] = "select_option"
    x: Coordinate
    y: Coordinate
    value: str = Field(min_length=1, max_length=200)


class Back(Wire):
    kind: Literal["back"] = "back"


class Home(Wire):
    kind: Literal["home"] = "home"


# The phone's side keys. Only a person taking the phone over presses them: the agent has
# no tool for them, and the cloud refuses one it proposes (rules.PERSON_ONLY_KINDS).


class VolumeUp(Wire):
    kind: Literal["volume_up"] = "volume_up"


class VolumeDown(Wire):
    kind: Literal["volume_down"] = "volume_down"


class Lock(Wire):
    """Locks the phone. A takeover ends with it: the Gateway does not wake a phone the
    person locked, so the next picture finds it locked (``device_locked``)."""

    kind: Literal["lock"] = "lock"


class LaunchApp(Wire):
    kind: Literal["launch_app"] = "launch_app"
    app_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.\-]+$")


class FindApp(Wire):
    """Searches the apps installed on the device by name. Nothing on the device changes."""

    kind: Literal["find_app"] = "find_app"
    query: str = Field(min_length=1, max_length=100)


ActionInput = Annotated[
    Tap
    | Swipe
    | LongPress
    | DoubleTap
    | Pinch
    | TypeText
    | SetPicker
    | SelectOption
    | Back
    | Home
    | VolumeUp
    | VolumeDown
    | Lock
    | LaunchApp
    | FindApp,
    Field(discriminator="kind"),
]
ActionKind = Literal[
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

# Capabilities a device declares. An action kind needs the capability of the same name.
Capability = ActionKind
