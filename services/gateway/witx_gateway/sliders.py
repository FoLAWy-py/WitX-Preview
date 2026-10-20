"""Reading the position of the sliders on the screen.

A slider is set by tapping or dragging where the agent thinks its position is, and a
stepped one (font size) takes the nearest step. What the agent aimed at and what the slider
took are two things, so the Gateway reads the position the phone reports and hands it back
with every screenshot. Pure functions, no device access.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from witx_gateway.options import Box, box_of

NAME_MAX = 200


@dataclass(frozen=True)
class SliderReading:
    name: str  # what the app calls it; "" if it has no name
    value: str  # the position it reports: "27%" on iOS, the step number on Android
    box: Box  # x, y, width, height in input points

    @property
    def center(self) -> tuple[float, float]:
        x, y, width, height = self.box
        return x + width / 2, y + height / 2


def parse_android_sliders(source: str) -> list[SliderReading]:
    """The SeekBars of an Android page source that are in view, top to bottom. The Settings
    sliders are not in the tree unless hidden views are allowed (the session asks for them);
    then the bar holds one node for its thumb, and the thumb's text is the position. A bar
    that carries the position itself is read as well. A bar with no position is skipped."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return []
    found: list[SliderReading] = []

    def visit(node: ET.Element) -> None:
        if node.tag != "android.widget.SeekBar":
            for child in node:
                visit(child)
            return
        thumbs = [t for t in node.iter("android.widget.SeekBar") if t is not node]
        reading = thumbs[0] if thumbs else node  # the thumb is inside the bar: not a slider
        value = reading.attrib.get("text", "").strip()
        box = box_of(node.attrib)
        if value and box is not None and reading.attrib.get("displayed") != "false":
            name = (
                node.attrib.get("content-desc") or reading.attrib.get("content-desc", "")
            ).strip(" ,")
            found.append(SliderReading(name[:NAME_MAX], value, box))

    visit(root)
    return sorted(found, key=lambda s: (s.box[1], s.box[0]))


def parse_percent(text: str) -> int | None:
    """ "27%" or "27": 27. Anything else (a slider that reports another kind of value): None."""
    found = re.fullmatch(r"\s*(\d{1,3})\s*%?\s*", text)
    return int(found.group(1)) if found and int(found.group(1)) <= 100 else None
