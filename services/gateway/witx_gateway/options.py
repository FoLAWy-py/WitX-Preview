"""Finding an option on the screen by the text it shows.

The Gateway reads the UI tree (Appium's page source), keeps the options that are on the
screen, and matches the text the agent asked for. Matching is strict on purpose: it is
what decides which thing gets tapped, so a near match is not a match. Pure functions,
no device access.
"""

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass

Box = tuple[float, float, float, float]  # x, y, width, height in input points


@dataclass(frozen=True)
class Option:
    text: str
    box: Box

    @property
    def center(self) -> tuple[float, float]:
        x, y, width, height = self.box
        return x + width / 2, y + height / 2


_ANDROID_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_options(source: str) -> list[Option]:
    """Everything with text on the screen, from an Android or iOS page source. Nodes that
    are not displayed are left out."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return []
    options: list[Option] = []
    for node in root.iter():
        attrs = node.attrib
        if attrs.get("displayed") == "false" or attrs.get("visible") == "false":
            continue
        text = next(
            (attrs[k] for k in ("text", "label", "content-desc", "name") if attrs.get(k)), ""
        )
        box = box_of(attrs)
        if text.strip() and box is not None:
            options.append(Option(text.strip(), box))
    return options


def box_of(attrs: dict[str, str]) -> Box | None:
    bounds = _ANDROID_BOUNDS.fullmatch(attrs.get("bounds", ""))
    if bounds:
        left, top, right, bottom = (float(v) for v in bounds.groups())
        return left, top, right - left, bottom - top
    try:
        return (float(attrs["x"]), float(attrs["y"]), float(attrs["width"]), float(attrs["height"]))
    except (KeyError, ValueError):
        return None


def normalize(text: str) -> str:
    """The text without what a list puts in front of it: flags, emoji and other symbols,
    case and spacing."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(c for c in folded if unicodedata.category(c) not in ("So", "Sk", "Cf", "Mn"))
    return " ".join(kept.split())


def find_option(
    options: list[Option], wanted: str, screen: tuple[float, float]
) -> tuple[str, Option | None]:
    """("found", option) when exactly one option on the screen shows this text, ("missing",
    None) when none does, ("ambiguous", None) when more than one does. Text that only
    contains the wanted text does not count: "China +86" is not "Hong Kong SAR China +852"."""
    target = normalize(wanted)
    width, height = screen
    matches = [
        o
        for o in options
        if normalize(o.text) == target and 0 <= o.center[0] < width and 0 <= o.center[1] < height
    ]
    # An item and the label inside it both show the text: one thing, the smaller box.
    distinct: list[Option] = []
    for o in sorted(matches, key=lambda m: m.box[2] * m.box[3]):
        if all(
            abs(o.center[1] - d.center[1]) > max(o.box[3], d.box[3]) / 2
            or abs(o.center[0] - d.center[0]) > max(o.box[2], d.box[2]) / 2
            for d in distinct
        ):
            distinct.append(o)
    if not distinct:
        return "missing", None
    if len(distinct) > 1:
        return "ambiguous", None
    return "found", distinct[0]


def signature(options: list[Option]) -> tuple[str, ...]:
    """What the screen shows, to tell whether a swipe moved it."""
    return tuple(normalize(o.text) for o in options)
