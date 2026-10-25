"""The device interface the executor drives, plus helpers shared by adapters."""

import io
import struct
import zlib
from dataclasses import dataclass
from typing import Protocol

from PIL import Image
from witx_contracts.common import Capability, Platform, Rotation, TypeMethod

from witx_gateway.mjpeg import StreamFrame
from witx_gateway.sliders import SliderReading

# Vision models downscale larger images before looking at them, and then answer in
# the coordinates of the image they saw. Screenshots are kept within this size so the
# pixels the model points at are the pixels of the stored screenshot.
MAX_SHORT_SIDE = 768
MAX_LONG_SIDE = 2048

# A finger's way across the screen: (x, y, milliseconds after the press), in input
# coordinates, in time order.
Path = list[tuple[float, float, int]]


class DeviceError(Exception):
    """A device operation failed. ``reason`` is a short code safe to report to the cloud."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WrongField(DeviceError):
    """The focused field is not where the agent said it was. Carries the field that
    has the focus, so the agent can see where it really is."""

    def __init__(self, field: "TextField") -> None:
        super().__init__("wrong_field")
        self.field = field


def point_in_box(point: tuple[float, float], box: tuple[float, float, float, float] | None) -> bool:
    """Whether a point lies in a field's box, with some room around it: a point aimed at
    the field on a screenshot is not exact, and the neighbouring fields are further off
    than this. An unreadable box cannot refuse anything."""
    if box is None:
        return True
    x, y, width, height = box
    room = max(12.0, 0.25 * min(width, height))
    return x - room <= point[0] <= x + width + room and y - room <= point[1] <= y + height + room


# A pinch: spread, the fingers are this far apart, of the screen's short side, and
# keep this far from its edges.
PINCH_SPREAD = 0.7
PINCH_MARGIN = 0.05


def pinch_fingers(x: float, scale: float, width: int, height: int) -> tuple[float, float, float]:
    """Where a pinch at ``x`` goes, in input coordinates: the fingers' midpoint, and how far
    each finger starts and ends from it. Spread they are PINCH_SPREAD of the short side
    apart, and the other end is ``scale`` times that (zoom in) or 1/``scale`` (zoom out).
    Near an edge the midpoint moves in, so that both fingers stay on the screen."""
    short = min(width, height)
    spread = PINCH_SPREAD * short / 2
    room = PINCH_MARGIN * short + spread
    middle = min(max(x, room), width - room)
    near = spread * min(scale, 1 / scale)
    return (middle, spread, near) if scale < 1 else (middle, near, spread)


@dataclass(frozen=True)
class Selection:
    """What select_option did: the option it tapped, as the screen shows it, or why it
    tapped nothing ("not_found", "ambiguous")."""

    selected: str | None = None
    problem: str | None = None


@dataclass(frozen=True)
class TextField:
    """The focused text field as read back after typing: what it holds, what the
    app calls it (content description, hint or label; "" if it has no name) and where it
    is, as x, y, width, height in input points."""

    text: str
    name: str = ""
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class DeviceState:
    """Current input coordinate space and foreground app, read from the device."""

    input_width: int
    input_height: int
    rotation: Rotation  # degrees clockwise from the natural orientation
    foreground_app: str


class Device(Protocol):
    @property
    def native_id(self) -> str: ...

    @property
    def platform(self) -> Platform: ...

    @property
    def name(self) -> str: ...

    @property
    def sensitive_apps(self) -> list[str]: ...

    @property
    def capabilities(self) -> list[Capability]: ...

    @property
    def swipe_coasts(self) -> bool:
        """Whether a list keeps moving after a swipe lets go of it, so the screenshot taken
        at once may show it mid-glide. Both phones do now that iOS swipes too are W3C
        actions that let go while the finger moves."""
        ...

    @property
    def settles_on_its_own(self) -> bool:
        """Whether the driver answers an input only once the app has settled (UiAutomator2
        waits up to 3 s for it to idle). One that answers at once (XCUITest, whose own
        wait of up to 2 s left WebDriverAgent hanging for 30 s on an app that never idles,
        D64) gets the screenshot after every input taken once the screen rests."""
        ...

    @property
    def type_methods(self) -> frozenset[TypeMethod]:
        """The ways it can put text into a field; another is refused before input."""
        ...

    @property
    def session_open(self) -> bool:
        """Whether the device answers without first opening a driver session (which can take
        minutes). A picture for someone watching a task is not worth opening one."""
        ...

    async def state(self) -> DeviceState:
        """The input space, its turn and the front app. Raises DeviceError("locked") when
        the screen is locked or dark."""
        ...

    async def wake(self) -> None:
        """Lights the screen for a person taking the phone over, and opens it when no
        passcode is set."""
        ...

    async def screenshot(self) -> bytes:
        """A PNG of the current screen, in the current orientation."""
        ...

    async def stream_frame(self, after: int, wait: float) -> StreamFrame | None:
        """The newest picture of the phone's own screen broadcast, once it is newer than
        ``after`` (waits up to ``wait`` seconds for one). None when the phone has no
        broadcast or it is not working: the caller takes screenshots instead."""
        ...

    async def tap(self, x: float, y: float) -> None: ...

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
        """Presses at (x1, y1), moves to (x2, y2) evenly in ``duration_ms``, stays there for
        ``hold_ms`` and lets go. A finger that holds still before letting go leaves a list
        where it is instead of coasting on; a held control (a joystick) acts all along.
        With ``path`` (points with the time each is reached, from the first at 0 ms to the
        last at ``duration_ms``) the finger follows it instead of the straight line."""
        ...

    async def long_press(self, x: float, y: float, duration_ms: int) -> None:
        """Presses (x, y) for ``duration_ms`` without moving."""
        ...

    async def double_tap(self, x: float, y: float) -> None: ...

    async def pinch(self, x: float, y: float, start: float, end: float) -> None:
        """Two fingers, at (x - start, y) and (x + start, y), move to x - end and x + end
        and lift."""
        ...

    async def check_text_target(self, point: tuple[float, float] | None = None) -> None:
        """Before typing: raises DeviceError("secure_field") when the focused field is a
        password or code field, and DeviceError("no_focused_field") when nothing is
        focused. Secrets are never typed by the agent. With ``point`` (input coordinates
        of the field the agent means) it raises WrongField when the focused field is
        elsewhere."""
        ...

    async def type_text(
        self, text: str, *, replace: bool = False, method: TypeMethod = "set", person: bool = False
    ) -> str | None:
        """Types ``text`` into the focused field at the cursor; with ``replace``, clears
        the field first so that ``text`` is all it holds. ``method`` is how: "set"
        into the field, "keys" tapped on the keyboard, "paste" through the field's menu.
        Returns why it stopped short ("no_candidate", "no_paste_menu" ...), or None: the
        field is read back either way. A password field that has the focus by now (it
        moved after ``check_text_target``) gets nothing from the agent: "secure_field";
        ``person``, typing for a person taking the phone over, does type into it. A field
        that is gone, or cannot be read, before anything is typed gets nothing either: the
        reason is returned, not raised (raised, the input's effect would be unknown)."""
        ...

    async def read_text_field(self, typed: str = "") -> TextField:
        """After typing ``typed``: the focused field, named without that text. Raises
        DeviceError("secure_field") for a password or code field, which is never read, and
        DeviceError("no_focused_field") when nothing is focused."""
        ...

    async def select_option(self, x: float, y: float, value: str) -> Selection:
        """Scrolls the list around (x, y) until exactly one option shows ``value`` and taps
        it; taps nothing if there is none, or several. A screen that cannot be read before
        the first swipe is a problem too: nothing was touched."""
        ...

    async def read_sliders(self) -> list[SliderReading]:
        """The sliders in view and the position each reports, top to bottom."""
        ...

    async def check_picker_target(self, x: float, y: float) -> None:
        """Before setting a picker: raises DeviceError("no_picker") when no picker wheel
        lies under (x, y)."""
        ...

    async def set_picker(self, x: float, y: float, value: str) -> str | None:
        """Turns the picker wheel under (x, y) to the item displayed as ``value``. Returns
        why it turned nothing when the wheel is gone, or cannot be read, by then
        ("no_picker"), else None."""
        ...

    async def back(self) -> None: ...

    async def home(self) -> None: ...

    async def volume_up(self) -> None: ...

    async def volume_down(self) -> None: ...

    async def lock(self) -> None:
        """Locks the phone (turns its screen off)."""
        ...

    async def check_app(self, app_id: str) -> None:
        """Before opening an app: raises DeviceError("app_not_installed") when the device
        has no app with this ID, so a wrong guess is refused before anything happens."""
        ...

    async def launch_app(self, app_id: str) -> None: ...

    async def list_apps(self) -> dict[str, str | None]:
        """The installed apps: ID to display name (None where the platform has none)."""
        ...

    async def close(self) -> None:
        """Releases what the Gateway opened for this device (on shutdown)."""
        ...


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise DeviceError("invalid_screenshot")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def same_screen(a: bytes, b: bytes) -> bool:
    """Whether two screenshots show the same screen. The status bar (it holds a clock) and
    the right edge (the scroll indicator, which fades after the list stops) are left out,
    and the rest is compared shrunk, so noise in the pixels does not count but a list that
    is still moving does."""

    def body(data: bytes) -> bytes:
        try:
            with Image.open(io.BytesIO(data)) as image:
                grey = image.convert("L")
                top = grey.height // 10
                right = grey.width - grey.width // 30
                shape = (max(1, right // 8), max(1, (grey.height - top) // 8))
                return grey.crop((0, top, right, grey.height)).resize(shape).tobytes()
        except (OSError, ValueError):
            raise DeviceError("invalid_screenshot") from None

    return a == b or body(a) == body(b)


def fit_png(data: bytes) -> bytes:
    """The screenshot scaled down to fit MAX_SHORT_SIDE x MAX_LONG_SIDE (never up)."""
    width, height = png_size(data)
    scale = min(1.0, MAX_SHORT_SIDE / min(width, height), MAX_LONG_SIDE / max(width, height))
    if scale == 1.0:
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            out = io.BytesIO()
            image.resize(size, Image.Resampling.LANCZOS).save(out, format="PNG")
    except (OSError, ValueError):
        raise DeviceError("invalid_screenshot") from None
    return out.getvalue()


def to_jpeg(png: bytes, quality: int = 70, max_side: int | None = None) -> bytes:
    """A JPEG of the screenshot, for a live picture; scaled down to ``max_side``."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            picture = image.convert("RGB")
            if max_side and max(picture.size) > max_side:
                picture.thumbnail((max_side, max_side))
            out = io.BytesIO()
            picture.save(out, format="JPEG", quality=quality)
    except (OSError, ValueError):
        raise DeviceError("invalid_screenshot") from None
    return out.getvalue()


def fit_jpeg(jpeg: bytes, max_side: int | None) -> tuple[bytes, int, int]:
    """A JPEG from a screen broadcast as it is, or shrunk to ``max_side`` when it is
    larger, with its size."""
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            width, height = image.size
            if not max_side or max(width, height) <= max_side:
                return jpeg, width, height
            picture = image.convert("RGB")
            picture.thumbnail((max_side, max_side))
            out = io.BytesIO()
            picture.save(out, format="JPEG", quality=70)
    except (OSError, ValueError):
        raise DeviceError("invalid_screenshot") from None
    return out.getvalue(), picture.width, picture.height


def solid_png(width: int, height: int, grey: int = 128) -> bytes:
    """A single-colour PNG (used by the fake device)."""
    row = b"\x00" + bytes([grey]) * width
    raw = row * height

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + payload))
        return struct.pack(">I", len(payload)) + kind + payload + crc

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
