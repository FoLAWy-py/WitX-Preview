"""Typing by tapping the on-screen keyboard, as a person does.

Some apps close their text box when text arrives any other way: Xiaohongshu's comment
box closes on WebDriverAgent's typing, whether it sets the value or sends key events,
and stays open under taps on the keyboard. The Gateway reads the keyboard from the page
source, plans the taps and checks each candidate it picks by its text. Chinese goes in
by pinyin and the candidate that shows the next characters; Latin letters by the
candidate that shows them as typed, which no autocorrection changes. Pure functions,
no device access.

What may be tapped is only what lies in the keyboard's own window (its keys and its
candidate bar), chosen by name: the return key, which sends in a chat, never is.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

from pypinyin import Style, lazy_pinyin

Box = tuple[float, float, float, float]  # x, y, width, height in input points

SHIFT = "shift"
MORE = "more"  # to the numbers and back to the letters
SPACE = "space"
DELETE = "delete"
NEXT_KEYBOARD = "Next keyboard"
MORE_CANDIDATES = "More suggestions"  # opens the candidates over the keys
CLOSE_CANDIDATES = "Close more suggestions"  # and puts the keys back
# Buttons of the keyboard's window outside the keyboard itself that are its own.
_BAR_BUTTONS = frozenset({NEXT_KEYBOARD, MORE_CANDIDATES, CLOSE_CANDIDATES})
# Keys tapped by name besides the characters: never the return key (send, go, search).
FUNCTION_KEYS = frozenset({SHIFT, MORE, SPACE, DELETE, NEXT_KEYBOARD})
# The page source with the attributes that are slow to work out left out: 0.3 s instead
# of 6 s for Safari with suggestions on a simulator.
SOURCE_EXCLUDED = "visible,accessible,enabled,index,hittable,traits,placeholderValue,value"

# Characters of a pinyin keyboard's candidates are typed this many at a time: enough
# that a phrase's candidate shows, few enough that the first candidates are about them.
HAN_CHUNK = 4
# A candidate's label is its text, then ", " and how it is said ("你好, 你们的你...");
# the letters as typed are split into syllables by this space ("ni\u2006hao").
_CANDIDATE_HINT = ", "
_SYLLABLE_GAP = "\u2006"
_HAN = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN = re.compile(r"[A-Za-z]+")
_CJK = re.compile("[\u3400-\u9fff]")


@dataclass(frozen=True)
class Board:
    """The keyboard on the screen: its keys by name, the candidates its bar shows, left
    to right, each with the text it puts in, and the words a Latin keyboard's bar offers
    by name ("“teh”", "the")."""

    keys: dict[str, Box]
    labels: dict[str, str]
    candidates: tuple[tuple[str, Box], ...]
    offered: dict[str, Box]

    @property
    def letters(self) -> bool:
        return "q" in self.keys or "Q" in self.keys

    @property
    def shifted(self) -> bool:
        """The next letter comes out a capital: the keys are named in capitals."""
        return "Q" in self.keys

    @property
    def chinese(self) -> bool:
        """A Chinese keyboard: its keys are named in Chinese ("删除", "数字")."""
        return any(_CJK.search(self.labels.get(name, "")) for name in (DELETE, MORE))

    @property
    def top(self) -> float:
        """Where the keyboard begins, its candidate bar included: a tap meant for the app
        must land above it."""
        boxes = [*self.keys.values(), *(box for _, box in self.candidates), *self.offered.values()]
        return min((box[1] for box in boxes), default=float("inf"))

    def key(self, char: str) -> Box | None:
        """The key that types ``char`` on this plane of the keyboard."""
        if char == " ":
            return self.keys.get(SPACE)
        return self.keys.get(char) if len(char) == 1 else None


def center(box: Box) -> tuple[float, float]:
    x, y, width, height = box
    return x + width / 2, y + height / 2


def _box(node: ET.Element) -> Box | None:
    try:
        box = tuple(float(node.attrib[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, ValueError):
        return None
    return box if box[2] > 0 and box[3] > 0 else None  # type: ignore[return-value]


def parse_board(source: str) -> Board | None:
    """The keyboard in an iOS page source, None when none is up. Only the window that
    holds the keyboard counts: an app's own lists and stickers above it are not its
    candidates. A candidate counts only where it is whole on the screen: the bar scrolls,
    and one beyond its edge, tapped, hit the button that opens the candidates over the
    keys."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return None
    window = next(
        (
            w
            for w in root.iter("XCUIElementTypeWindow")
            if w.find(".//XCUIElementTypeKeyboard") is not None
        ),
        None,
    )
    if window is None:
        return None
    board = window.find(".//XCUIElementTypeKeyboard")
    frame = _box(board) if board is not None else None
    if board is None or frame is None:
        return None
    keys: dict[str, Box] = {}
    labels: dict[str, str] = {}
    candidates: list[tuple[str, Box]] = []
    offered: dict[str, Box] = {}
    # Keys are what the keyboard itself holds: a word the bar offers ("I") is a button
    # named like a key, and was tapped for one.
    inside = {id(node) for node in board.iter()}
    for node in window.iter():
        box = _box(node)
        if box is None:
            continue
        name = node.get("name") or ""
        if node.tag in ("XCUIElementTypeKey", "XCUIElementTypeButton") and name:
            if id(node) in inside or name in _BAR_BUTTONS:
                if name not in keys:
                    keys[name] = box
                    labels[name] = node.get("label") or ""
            else:
                offered.setdefault(name, box)
        elif node.tag == "XCUIElementTypeCell" and node.get("label"):
            text = str(node.get("label")).split(_CANDIDATE_HINT, 1)[0]
            candidates.append((text, box))
    button = keys.get(MORE_CANDIDATES) or keys.get(CLOSE_CANDIDATES)
    shown = [
        (text, box)
        for text, box in candidates
        if box[0] >= frame[0]
        and box[0] + box[2] <= frame[0] + frame[2]
        and not (button and _overlap(box, button))
    ]
    shown.sort(key=lambda c: (c[1][1], c[1][0]))
    return Board(keys, labels, tuple(shown), offered)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


@dataclass(frozen=True)
class Token:
    """A piece of the text, typed one way: Chinese by pinyin, Latin letters as a word,
    anything else a key at a time."""

    kind: Literal["han", "latin", "char"]
    text: str


def tokens(text: str) -> list[Token]:
    found: list[Token] = []
    at = 0
    while at < len(text):
        for kind, pattern in (("han", _HAN), ("latin", _LATIN)):
            match = pattern.match(text, at)
            if match:
                found.append(Token(kind, match.group()))  # type: ignore[arg-type]
                at = match.end()
                break
        else:
            found.append(Token("char", text[at]))
            at += 1
    return found


def untypable(text: str) -> str | None:
    """The first character no keyboard key types (a new line, an emoji), or None."""
    for char in text:
        if char in "\r\n\t" or (not char.isprintable()) or ord(char) > 0xFFFF:
            return char
    return None


def syllables(han: str) -> list[str]:
    """The pinyin of each character, as a pinyin keyboard takes it ("ü" as "v"), read
    word by word so that a character with two readings gets the one its word has."""
    return [s.lower() for s in lazy_pinyin(han, style=Style.NORMAL, v_to_u=False)]


_COMPOSED = re.compile("^[a-z\u2006']+$")


def composing(board: Board) -> int:
    """How many letters a pinyin keyboard is composing: its bar shows them as typed
    ("xian\u2006gao"), and after a word only Chinese predictions."""
    typed = (text for text, _ in board.candidates if _COMPOSED.match(text))
    return len(next(typed, "").replace(_SYLLABLE_GAP, ""))


def pick(pending: str, candidates: tuple[tuple[str, Box], ...]) -> tuple[str, Box] | None:
    """The candidate that puts in the most of what is still to type, from its start."""
    best: tuple[str, Box] | None = None
    for text, box in candidates:
        if text and pending.startswith(text) and (best is None or len(text) > len(best[0])):
            best = (text, box)
    return best


def pick_as_typed(word: str, candidates: tuple[tuple[str, Box], ...]) -> tuple[str, Box] | None:
    """The candidate that puts in the letters exactly as typed."""
    return next(
        ((text, box) for text, box in candidates if text.replace(_SYLLABLE_GAP, "") == word),
        None,
    )
