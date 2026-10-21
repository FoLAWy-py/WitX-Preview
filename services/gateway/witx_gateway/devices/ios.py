"""iOS through a local Appium server (XCUITest driver + WebDriverAgent)."""

import asyncio
import contextlib
import itertools
import json
import logging
import re
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import httpx
from witx_contracts.common import Capability, Platform, Rotation, TypeMethod
from witx_contracts.gateway import MAX_SLIDERS

from witx_gateway import keyboard
from witx_gateway.devices.appium import DOWN, PRESS_MS, UP, AppiumDevice, Identities, move, pause
from witx_gateway.devices.base import DeviceError, TextField
from witx_gateway.sliders import NAME_MAX, SliderReading, parse_percent

log = logging.getLogger(__name__)

# Simulators have UUIDs; real devices have UDIDs like 00000000-0000000000000001.
SIMULATOR_UDID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$", re.I)


# WebDriverAgent answers the front app's bundle ID from a snapshot of that app's element
# tree, as deep as this setting allows (its default is 50 levels), and it places every
# point of a gesture (each move of a W3C action, a tap at a coordinate) on the app's frame,
# which it reads from such a snapshot too, once per point. On a busy screen (a chat with
# tens of thousands of elements) one snapshot takes seconds to tens of seconds, and every
# other command waits behind it; a person's drag, sampled every 16 ms, has dozens of
# points, so even a light screen's 100 ms snapshot made a drag of under a second take 4 to
# 8 s. The bundle ID and the frame need only the root, so the tree is cut to one level for
# those commands and let out again after them: finding the focused field, the sliders, the
# picker wheels and the page source all need the whole tree.
SNAPSHOT_DEPTH = 50
SNAPSHOT_DEPTH_ROOT_ONLY = 1

# An agent that lost its rights is renewed at most this often: renewing starts it again
# (seconds to tens of seconds), and a phone it does not help must not be renewed over and
# over.
RENEW_BACKOFF = 120.0
# WebDriverAgent's app on a real iPhone, by its bundle name: its bundle ID is the team's.
AGENT_APP = "/WebDriverAgentRunner-Runner.app/"

# A slider is thin: a point aimed at it on the screenshot may be this far off (points).
SLIDER_ROOM = 20.0
# Where in a step's share of the range to aim, in percent, one try after the other.
SLIDER_OFFSETS = (0.5, 0.3, 0.7, 0.4, 0.6, 0.5)
SLIDER_TRIES = len(SLIDER_OFFSETS)

# Tapping the keyboard. A key is held this long; the next one is pressed after
# this gap: a gap no longer than the hold let two fingers touch at once, and the keys came
# out in the wrong order. Shift, and the tap just before it, are held longer, and shift
# is waited for: with the tap before it held as briefly as a key, a shift while pinyin
# was being composed left the next letter lowercase. A tapped candidate is let settle
# before the next letters.
KEY_MS = 30
KEY_GAP_MS = 70
SHIFT_MS = 120
SHIFT_GAP_MS = 300
SHIFT_PAUSE = 0.2
CANDIDATE_GAP_MS = 200
DELETE_GAP_MS = 120
# Taps in one W3C action: one finger each.
TAPS_PER_ACTION = 12
# The keyboard redraws its candidates after the last key, before it is read again.
BOARD_SETTLE = 0.15
# Reads of the keyboard while the candidates opened over the keys close (some 0.5 s).
PANEL_READS = 4
# The most text tapped in.
KEYS_MOST = 300
# Keyboards the globe key is tapped through to find a pinyin one.
KEYBOARDS_MOST = 4
# The edit menu: shown a moment after the tap; a long press opens it too.
MENU_SETTLE = 0.6
MENU_HOLD_MS = 800
# The edit menu's Paste as it is shown: only the item named exactly that, never one that
# pastes and submits ("Paste and Search"). The page source holds menus shown before too,
# hidden: the first Paste in it on a real iPhone was such a one, and a tap there hit the
# AutoFill of the menu in view.
PASTE_ITEM = '**/XCUIElementTypeMenuItem[`visible == 1 AND label IN {"Paste", "粘贴", "貼上"}`]'
MENU_ITEM = "**/XCUIElementTypeMenuItem[`visible == 1`]"
# A paste lands a moment after Paste is tapped: the field is read this often, this far apart.
PASTE_READS = 8
PASTE_SETTLE = 0.25
# Emptying a field: a tap this far inside its bottom right corner puts the cursor at its
# end, and the delete key is held this long, in rounds: at least this many, one more for
# every so many characters (a 1.5 s hold deleted 12 on a real iPhone).
FIELD_END_INSET = 6.0
DELETE_HOLD_MS = 2500
DELETE_ROUNDS = 3
DELETE_PER_ROUND = 12

Tap = tuple[float, float, int, int]  # x, y, how long it is held, the gap after it (ms)


class _Stop(Exception):
    """Typing on the keyboard stopped short, and why."""

    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


def _tap(box: keyboard.Box, gap: int = KEY_GAP_MS, hold: int = KEY_MS) -> Tap:
    return (*keyboard.center(box), hold, gap)


class _KeyTyping:
    """One text tapped in on the keyboard: Chinese on a pinyin keyboard, Latin letters on
    another one, turned to with the globe key. A candidate that ends a word is tapped with
    the letters of the next (``carry``): one W3C action fewer per word."""

    def __init__(self, device: "IOSDevice", board: keyboard.Board) -> None:
        self.device = device
        self.board = board
        self.carry: list[Tap] = []
        # Whether ``board`` is still the page on the screen: a character key may turn the
        # keyboard back to its letters by itself.
        self.current = True
        # The space after a word whose letters were kept as typed is in already.
        self.spaced = False

    async def press(self, taps: list[Tap]) -> None:
        await self.device._press([*self.carry, *taps])
        self.carry = []

    async def read(self) -> keyboard.Board:
        """The keyboard as it is once every tap still to send has been sent."""
        await self.flush()
        self.board = await self.device._board()
        self.current = True
        return self.board

    async def flush(self) -> None:
        if self.carry:
            await self.press([])

    async def use(self, *, chinese: bool) -> bool:
        """Turns with the globe key to a pinyin keyboard, or to one with Latin letters;
        False when the phone has none."""
        if not self.current:
            await self.read()
        for _ in range(KEYBOARDS_MOST + 1):
            if self.board.chinese == chinese and (
                self.board.letters or keyboard.MORE in self.board.keys
            ):
                return True
            globe = self.board.keys.get(keyboard.NEXT_KEYBOARD)
            if globe is None:
                return False
            await self.press([_tap(globe)])
            await self.read()
        return False

    def key(self, name: str) -> keyboard.Box:
        box = self.board.keys.get(name)
        if box is None:
            raise _Stop("not_on_keyboard")
        return box

    async def letters(self) -> None:
        """Onto the keyboard's letters, from its numbers or symbols. Candidates opened
        over the keys stay open until the last of a word's is picked: that pick is sent,
        and the keys are waited for."""
        if not self.current or (self.carry and not self.board.letters):
            await self.read()
        for _ in range(PANEL_READS):
            if self.board.letters or keyboard.CLOSE_CANDIDATES not in self.board.keys:
                break
            await self.read()
        if not self.board.letters:
            await self.press([_tap(self.key(keyboard.MORE))])
            if not (await self.read()).letters:
                raise _Stop("not_on_keyboard")

    def spell(self, word: str, shifted: bool | None = None) -> list[Tap]:
        """The taps for a word, shift before each capital. A keyboard that capitalises
        by itself (at the start of a sentence its keys are capitals) has shift tapped
        off first for a small letter."""
        taps: list[Tap] = []
        shifted = self.board.shifted if shifted is None else shifted
        for char in word:
            if char.isupper() != shifted:
                taps.append(_tap(self.key(keyboard.SHIFT), SHIFT_GAP_MS, SHIFT_MS))
            box = self.board.keys.get(char.lower()) or self.board.keys.get(char.upper())
            if box is None:
                raise _Stop("not_on_keyboard")
            taps.append(_tap(box))
            shifted = False
        return taps

    async def type(self, part: keyboard.Token, following: keyboard.Token | None) -> None:
        if part.kind == "han":
            await self.han(part.text)
        elif part.kind == "latin":
            await self.latin(part.text, following)
        else:
            await self.char(part.text)

    async def han(self, text: str) -> None:
        """A few characters' pinyin at a time, then the candidates that show them, from
        the bar or, when none in view does, from the bar opened over the keys. When none
        there does either, the piece is taken back and typed again in halves: all of it,
        since a delete after some of it was picked first puts that back to pinyin."""
        if not await self.use(chinese=True):
            raise _Stop("no_pinyin_keyboard")
        said = keyboard.syllables(text)
        at, size = 0, keyboard.HAN_CHUNK
        while at < len(text):
            chunk = text[at : at + size]
            letters = "".join(said[at : at + len(chunk)])
            await self.letters()
            await self.press(self.spell(letters))
            done = picks = 0
            while done < len(chunk):
                found = keyboard.pick(chunk[done:], (await self.read()).candidates)
                if found is None:
                    found = await self.open_candidates(chunk[done:])
                    if found is None:
                        break
                    done, picks = done + len(found[0]), picks + 1
                    continue
                done, picks = done + len(found[0]), picks + 1
                tap = _tap(found[1], CANDIDATE_GAP_MS)
                if done < len(chunk):
                    await self.press([tap])
                else:
                    self.carry.append(tap)
            if done == len(chunk):
                at, size = at + len(chunk), keyboard.HAN_CHUNK
                continue
            await self.close_candidates()
            await self.take_back(len(letters) + picks)
            if len(chunk) == 1:
                raise _Stop("no_candidate")
            size = len(chunk) // 2

    async def open_candidates(self, pending: str) -> tuple[str, keyboard.Box] | None:
        """A candidate further along the bar (a rare character): the bar is opened over
        the keys and the candidate tapped there, which closes it again. Not there either,
        it is closed."""
        more = self.board.keys.get(keyboard.MORE_CANDIDATES)
        if more is None:
            return None
        await self.press([_tap(more)])
        found = keyboard.pick(pending, (await self.read()).candidates)
        if found is not None:
            await self.press([_tap(found[1], CANDIDATE_GAP_MS)])
            self.current = False
        else:
            await self.close_candidates()
        return found

    async def close_candidates(self) -> None:
        """Closes candidates opened over the keys, and waits for the keys to be back."""
        close = (await self.read()).keys.get(keyboard.CLOSE_CANDIDATES)
        if close is None:
            return
        await self.press([_tap(close)])
        for _ in range(PANEL_READS):
            if keyboard.DELETE in (await self.read()).keys:
                break

    async def latin(self, word: str, following: keyboard.Token | None) -> None:
        """A word on a Latin keyboard, read first (it capitalises by itself at the start
        of a sentence). Before the space or mark after it, which would let autocorrection
        change it, the word as typed is tapped where the bar offers it ("“teh”" next to
        "the"): it goes in with a space, taken back before a mark. With no Latin keyboard,
        on the pinyin one, by the candidate that shows the letters as typed."""
        if await self.use(chinese=False):
            await self.read()
            await self.letters()
            await self.write(word)
            if following is None or following.kind != "char" or following.text.isalnum():
                return
            kept = (await self.read()).offered.get(f"\u201c{word}\u201d")
            if kept is not None:
                self.carry.append(_tap(kept, CANDIDATE_GAP_MS))
                if following.text == " ":
                    self.spaced = True
                else:
                    self.carry.append(_tap(self.key(keyboard.DELETE)))
            return
        await self.letters()
        await self.write(word)
        found = keyboard.pick_as_typed(word, (await self.read()).candidates)
        if found is None:
            await self.undo(len(word))
        self.carry.append(_tap(found[1], CANDIDATE_GAP_MS))

    async def write(self, word: str) -> None:
        """A word's letters, a capital inside it ("OK", "iPhone") starting a W3C action
        of its own a moment later: in one action with the letters before it, a second
        shift sometimes did not take."""
        cuts = [0, *(n for n, char in enumerate(word) if n and char.isupper()), len(word)]
        for n, (start, end) in enumerate(itertools.pairwise(cuts)):
            if n:
                await asyncio.sleep(SHIFT_PAUSE)
            await self.press(self.spell(word[start:end], None if n == 0 else False))

    async def undo(self, letters: int) -> NoReturn:
        """Takes back the letters still being composed, and stops."""
        await self.take_back(letters)
        raise _Stop("no_candidate")

    async def take_back(self, letters: int) -> None:
        """Deletes the letters being composed. A keyboard that predicts as it goes let some
        deletes go by: what is still composed is read and deleted again, twice at most."""
        for _ in range(3):
            if letters <= 0:
                return
            await self.press([_tap(self.key(keyboard.DELETE), DELETE_GAP_MS)] * letters)
            letters = keyboard.composing(await self.read())
        raise _Stop("no_candidate")

    async def char(self, char: str) -> None:
        """A key from whichever page of the keyboard has it. After some (a Chinese comma)
        the keyboard turns back to its letters by itself, so the page is read again unless
        it is known to stay: after a digit on the numbers, a space on the letters. A
        punctuation mark the candidate bar offers after a word is tapped there. A mark
        only the other keyboard has (an ASCII comma on a pinyin one) is typed there."""
        if self.spaced and char == " ":
            self.spaced = False
            return
        self.spaced = False
        if self.current and (box := self.board.key(char)) is not None:
            self.carry.append(_tap(box))
            self.current = (char == " " and self.board.letters) or (
                char.isdigit() and not self.board.letters
            )
            return
        await self.flush()
        board = await self.read()
        offered = next((b for text, b in board.candidates if text == char), None)
        if offered is not None:
            self.carry.append(_tap(offered, CANDIDATE_GAP_MS))
            return
        found = await self.find(char)
        if found is None and await self.use(chinese=not self.board.chinese):
            found = await self.find(char)
        if found is None:
            raise _Stop("not_on_keyboard")
        self.carry.append(_tap(found))
        self.current = char.isdigit() and not self.board.letters

    async def find(self, char: str) -> keyboard.Box | None:
        """The key for ``char`` on this keyboard's page, turning its pages (letters,
        numbers, symbols) up to twice."""
        board = await self.read()
        turns = (
            (keyboard.MORE, keyboard.SHIFT) if board.letters else (keyboard.SHIFT, keyboard.MORE)
        )
        for turn in turns:
            if board.key(char) is not None:
                break
            await self.press([_tap(self.key(turn))])
            board = await self.read()
        return board.key(char)


class IOSDevice(AppiumDevice):
    platform: Platform = "ios"
    # A W3C swipe lets go while the finger still moves, and the list glides on.
    swipe_coasts = True
    # WebDriverAgent's own wait for the app to settle after a gesture is turned off (see
    # _session_capabilities), so the Gateway waits for the screen to rest instead.
    settles_on_its_own = False
    type_methods: frozenset[TypeMethod] = frozenset({"set", "keys", "paste"})
    _name_attributes = ("label", "name", "placeholderValue")
    # iOS has no system back button; the model uses visible controls instead.
    _capabilities: ClassVar[list[Capability]] = [
        "tap",
        "swipe",
        "long_press",
        "double_tap",
        "pinch",
        "type_text",
        "set_picker",
        "select_option",
        "home",
        "lock",
        "launch_app",
        "find_app",
    ]

    def __init__(
        self,
        *,
        udid: str,
        name: str,
        appium_url: str,
        sensitive_apps: list[str] | None = None,
        ports: dict[str, int] | None = None,
        wda_url: str | None = None,
        capabilities: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        identities: Identities | None = None,
        keep_warm: bool = False,
    ) -> None:
        super().__init__(
            native_id=udid,
            name=name,
            appium_url=appium_url,
            sensitive_apps=sensitive_apps,
            ports=ports,
            capabilities=capabilities,
            transport=transport,
            identities=identities,
            keep_warm=keep_warm,
        )
        self._wda_url = wda_url
        # The screen held upright, in points, and the session it was read in (see
        # ``_screen_size``).
        self._upright: tuple[tuple[int, int], str] | None = None
        self._renewed_at = -RENEW_BACKOFF  # when its agent was last renewed (``_dark``)

    def configure(
        self,
        *,
        name: str,
        sensitive_apps: list[str],
        capabilities: dict[str, Any],
        keep_warm: bool,
        wda_url: str | None = None,
    ) -> None:
        super().configure(
            name=name, sensitive_apps=sensitive_apps, capabilities=capabilities, keep_warm=keep_warm
        )
        self._wda_url = wda_url

    @property
    def signed(self) -> bool:
        """Whether WebDriverAgent can run on a real iPhone: signed with a team, or
        already running at ``wda_url``."""
        return bool(self._wda_url or "appium:xcodeOrgId" in self._extra_capabilities)

    def _session_capabilities(self) -> dict[str, Any]:
        caps: dict[str, Any] = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            # After a gesture WebDriverAgent waits up to 2 s for the app to go idle (its
            # animation cool-off). An app that never idles (a video, a feed) makes every
            # gesture 2 s longer, and on a real iPhone XCTest, having given up on the
            # idle notification, left the driver not answering until the next command
            # came, 30 s later. The Gateway waits for the screen to rest instead
            # (``settles_on_its_own``).
            "appium:settings[animationCoolOffTimeout]": 0,
        }
        if self._wda_url:
            caps["appium:webDriverAgentUrl"] = self._wda_url
        return caps

    async def _dark(self) -> NoReturn:
        """The agent sees nothing of the screen. On a real iPhone that may be an agent that
        lost its rights while the phone was locked with a passcode, which it keeps after
        someone unlocks the phone: its screenshots fail with XCTest's code 41. Then the
        phone itself is asked (``devicectl``, outside XCTest) whether it still needs its
        passcode: if not, the session ends and the agent's process on the phone is stopped
        (``stop_agent``), at most every RENEW_BACKOFF seconds, and the call fails as
        "session_lost": the next one opens a new session, and Appium starts the agent again.
        Otherwise, and on a simulator, the screen is locked or dark until someone wakes it."""
        if SIMULATOR_UDID.match(self.native_id) or self._wda_url:
            await super()._dark()
        try:
            await self._session_call("GET", "/screenshot")
            unauthorized = False
        except DeviceError as error:
            unauthorized = error.reason == "agent_unauthorized"
        now = time.monotonic()
        if (
            unauthorized
            and now - self._renewed_at >= RENEW_BACKOFF
            and await passcode_required(self.native_id) is False
        ):
            self._renewed_at = now
            self.locked = False
            log.warning("renewing an agent that lost its rights", extra={"device": self.native_id})
            await self._end_session()
            if not await stop_agent(self.native_id):
                log.warning("could not stop the agent", extra={"device": self.native_id})
            raise DeviceError("session_lost")
        await super()._dark()

    async def _identity_of(self) -> str:
        info = await self._script("mobile: deviceInfo")
        if not isinstance(info, dict):
            raise DeviceError("wrong_device")
        simulator = info.get("isSimulator") is True
        if simulator != bool(SIMULATOR_UDID.match(self.native_id)):
            raise DeviceError("wrong_device")
        if simulator:
            # A simulator is reached by its UDID, so that is who it is. What the device
            # reports as its own ID is WebDriverAgent's identifierForVendor, which iOS
            # changes when the agent is reinstalled: a new value there is not a new device.
            return f"simulator:{self.native_id}"
        udid = (info.get("lockdownInfo") or {}).get("UniqueDeviceID")
        if udid != self.native_id:
            raise DeviceError("wrong_device")
        return f"device:{udid}"

    def _outdated(self, pinned: str, seen: str) -> bool:
        # Simulators were pinned as simulator:<identifierForVendor> before.
        return pinned.startswith("simulator:") and seen.startswith("simulator:")

    @contextlib.asynccontextmanager
    async def _root_only(self) -> AsyncIterator[None]:
        """A command that needs no more of the element tree than the app's root runs with
        the snapshot cut to that; the depth is put back after it, also when it fails."""
        await self._settings(snapshotMaxDepth=SNAPSHOT_DEPTH_ROOT_ONLY)
        try:
            yield
        finally:
            await self._settings(snapshotMaxDepth=SNAPSHOT_DEPTH)

    async def _foreground_app(self) -> str:
        async with self._root_only():
            app = await self._script("mobile: activeAppInfo")
        try:
            return str(app["bundleId"])
        except (KeyError, TypeError):
            raise DeviceError("unknown_geometry") from None

    async def _touch(self, *fingers: list[dict[str, Any]]) -> None:
        async with self._root_only():
            await super()._touch(*fingers)

    async def _screen_size(self, rotation: Rotation) -> tuple[int, int]:
        """The screen in points, the way round the front app is held. Read from the screen
        itself, not from the front app's window: that would be a snapshot of the app's
        whole element tree, and a phone's app fills the screen anyway. The screen
        is read once per session (reading it takes a quarter of a second, the time to find
        the status bar) and then only turned with the rotation: WebDriverAgent turns both
        by the front app's orientation, and a phone's screen does not change size."""
        session = await self._session()
        if self._upright is None or self._upright[1] != session:
            info = await self._script("mobile: deviceScreenInfo")
            try:
                size = info["screenSize"]
                width, height = int(size["width"]), int(size["height"])
            except (KeyError, TypeError, ValueError):
                raise DeviceError("unknown_geometry") from None
            self._upright = ((min(width, height), max(width, height)), session)
        (short, long), _ = self._upright
        return (long, short) if rotation in (90, 270) else (short, long)

    async def tap(self, x: float, y: float) -> None:
        # A W3C press, like the other gestures: XCUITest's own tap (``mobile: tap``) took
        # 11 s once on a real iPhone playing video, the W3C gestures around it 3 s.
        await self._touch([move(x, y), DOWN, pause(PRESS_MS), UP])

    async def check_text_target(self, point: tuple[float, float] | None = None) -> None:
        element_id = await self._focused_element()
        kind = await self._session_call("GET", f"/element/{element_id}/attribute/type")
        if kind == "XCUIElementTypeSecureTextField":
            raise DeviceError("secure_field")
        await self._check_field_at(element_id, point)

    async def type_text(
        self, text: str, *, replace: bool = False, method: TypeMethod = "set"
    ) -> str | None:
        if method == "keys":
            return await self._type_keys(text, replace)
        if method == "paste":
            return await self._paste(text, replace)
        element_id = await self._focused_element()
        if replace:
            await self._session_call("POST", f"/element/{element_id}/clear")
        await self._session_call("POST", f"/element/{element_id}/value", {"text": text})
        return None

    async def _field_holds(self, element_id: str) -> str:
        """The field's text; an empty field answers with its placeholder."""
        attribute = f"/element/{element_id}/attribute/"
        value = str(await self._session_call("GET", attribute + "value") or "")
        placeholder = str(await self._session_call("GET", attribute + "placeholderValue") or "")
        return "" if value == placeholder else value

    async def _source(self) -> str:
        return str(
            await self._script(
                "mobile: source",
                {"format": "xml", "excludedAttributes": keyboard.SOURCE_EXCLUDED},
            )
            or ""
        )

    async def _press(self, taps: list[Tap]) -> None:
        """Taps one after the other, each with its own finger, a few to a W3C action. One
        finger tapping again moved there as if pressed: every key after the first came
        out twice on a simulator."""
        taps = [
            (x, y, SHIFT_MS, gap) if n + 1 < len(taps) and taps[n + 1][2] == SHIFT_MS else tap
            for n, tap in enumerate(taps)
            for x, y, _, gap in [tap]
        ]
        for start in range(0, len(taps), TAPS_PER_ACTION):
            fingers, at = [], 0
            for x, y, hold, gap in taps[start : start + TAPS_PER_ACTION]:
                fingers.append([*([pause(at)] if at else []), move(x, y), DOWN, pause(hold), UP])
                at += hold + gap
            await self._touch(*fingers)

    async def _board(self) -> keyboard.Board:
        await asyncio.sleep(BOARD_SETTLE)
        board = keyboard.parse_board(await self._source())
        if board is None:
            raise _Stop("no_keyboard")
        return board

    async def _type_keys(self, text: str, replace: bool) -> str | None:
        """Taps the text in on the keyboard. Chinese and Latin letters go in through
        a pinyin keyboard's candidates, picked by their text; other characters a key at a
        time, from whichever of the keyboard's pages has them."""
        if len(text) > KEYS_MOST:
            return "too_long_for_keys"
        if keyboard.untypable(text) is not None:
            return "not_on_keyboard"
        try:
            typing = _KeyTyping(self, await self._board())
            parts = keyboard.tokens(text)
            if any(p.kind == "han" for p in parts) and not await typing.use(chinese=True):
                return "no_pinyin_keyboard"
            element_id = await self._focused_element()
            if replace and await self._field_holds(element_id):
                await self._delete_all(element_id)
                await typing.read()
            for n, part in enumerate(parts):
                await typing.type(part, parts[n + 1] if n + 1 < len(parts) else None)
            await typing.flush()
        except _Stop as stop:
            return stop.problem
        return None

    async def _paste(self, text: str, replace: bool) -> str | None:
        """Pastes through the field's own menu, as a person does: the app reads nothing
        from the pasteboard by itself, so iOS asks no one. The pasteboard is set from this
        Mac and read back, and emptied afterwards, also when the paste failed: it must not
        keep the text for other apps. Only into an empty field: a tap would put the cursor
        somewhere in its text; what a field holds is deleted first on ``replace``. Done
        when the field holds text: a paste of something else (a picture) puts none in."""
        element_id = await self._focused_element()
        box = await self._field_box(element_id)
        if box is None:
            return "no_field_box"
        holds = await self._field_holds(element_id)
        if holds and not replace:
            return "field_not_empty"
        try:
            if holds:
                await self._delete_all(element_id)
            if not await _host_pasteboard(self.native_id, text):
                return "pasteboard_not_set"
            paste = await self._menu(keyboard.center(box))
            if paste is None:
                return "no_paste_menu"
            await self._press([(*keyboard.center(paste), KEY_MS, 0)])
            for _ in range(PASTE_READS):
                await asyncio.sleep(PASTE_SETTLE)
                if await self._field_holds(element_id):
                    return None
            return "paste_not_taken"
        except _Stop as stop:
            return stop.problem
        finally:
            await self._close_menu(keyboard.center(box))
            await _host_pasteboard(self.native_id, "")

    async def _delete_all(self, element_id: str) -> None:
        """Deletes all the field holds with the keyboard's delete key, held down after a tap
        at the field's end puts the cursor there; in rounds, each read back, as a hold
        deletes only what is before the cursor and a long text took more than one. The
        edit menu's Select All was not used: a real iPhone (iOS 27.2) offered none, in
        Xiaohongshu's comment box or in Notes."""
        held = await self._field_holds(element_id)
        board = await self._board()
        x, y, width, height = await self._field_box(element_id) or (0.0, 0.0, 0.0, 0.0)
        end = (x + width - FIELD_END_INSET, min(y + height, board.top) - FIELD_END_INSET)
        delete = board.keys.get(keyboard.DELETE)
        if width <= 0 or end[1] <= y or delete is None:
            raise _Stop("field_not_cleared")
        try:
            for _ in range(DELETE_ROUNDS + len(held) // DELETE_PER_ROUND):
                await self._press([(*end, KEY_MS, KEY_GAP_MS), _tap(delete, 0, DELETE_HOLD_MS)])
                if not await self._field_holds(element_id):
                    return
            raise _Stop("field_not_cleared")
        finally:
            await self._close_menu(end)

    async def _close_menu(self, point: tuple[float, float]) -> None:
        """Hides an edit menu typing left in view: a tap where it was opened hides it again.
        Left there, it covers what is under it (a comment box's own bar), and the agent
        spent ten inputs tapping around one to get it away."""
        with contextlib.suppress(DeviceError):
            for _ in range(2):
                shown = await self._session_call(
                    "POST", "/elements", {"using": "-ios class chain", "value": MENU_ITEM}
                )
                if not shown:
                    return
                await self._press([(*point, KEY_MS, 0)])
                await asyncio.sleep(MENU_SETTLE)

    async def _menu(self, point: tuple[float, float]) -> keyboard.Box | None:
        """The field's edit menu's Paste, opened as a person would: a long press on the
        focused field, which shows the menu, then taps, each of which shows it or hides it
        again."""
        for hold in (MENU_HOLD_MS, KEY_MS, KEY_MS):
            await self._touch([move(*point), DOWN, pause(hold), UP])
            await asyncio.sleep(MENU_SETTLE)
            found = await self._session_call(
                "POST", "/elements", {"using": "-ios class chain", "value": PASTE_ITEM}
            )
            for item in found if isinstance(found, list) else []:
                element_id = next(iter(item.values()), None) if isinstance(item, dict) else None
                box = await self._field_box(str(element_id)) if element_id else None
                if box is not None and box[2] > 0 and box[3] > 0:
                    return box
        return None

    async def read_text_field(self, typed: str = "") -> TextField:
        element_id = await self._focused_element()
        kind = await self._session_call("GET", f"/element/{element_id}/attribute/type")
        if kind == "XCUIElementTypeSecureTextField":
            raise DeviceError("secure_field")
        value = await self._session_call("GET", f"/element/{element_id}/attribute/value")
        text = str(value or "")
        return TextField(
            text=text,
            name=await self._field_name(element_id, self._name_attributes, typed),
            box=await self._field_box(element_id),
        )

    async def read_sliders(self) -> list[SliderReading]:
        """The sliders in view. Looked up by type, not by reading the whole UI tree: that
        takes up to seconds on a busy screen, this a few tens of milliseconds."""
        found = await self._session_call(
            "POST", "/elements", {"using": "class name", "value": "XCUIElementTypeSlider"}
        )
        readings: list[SliderReading] = []
        for slider in found[:MAX_SLIDERS] if isinstance(found, list) else []:
            element_id = next(iter(slider.values()), None) if isinstance(slider, dict) else None
            if not element_id:
                continue
            attribute = f"/element/{element_id}/attribute/"
            if await self._session_call("GET", attribute + "visible") not in ("true", True, "1", 1):
                continue
            value = str(await self._session_call("GET", attribute + "value") or "").strip()
            box = await self._field_box(str(element_id))
            if value and box is not None:
                name = await self._field_name(str(element_id), ("label", "name"))
                readings.append(SliderReading(name[:NAME_MAX], value, box))
        return sorted(readings, key=lambda s: (s.box[1], s.box[0]))

    async def _control_at(self, x: float, y: float) -> tuple[str, str]:
        """The picker wheel or slider under a point, as ("wheel" | "slider", element ID).
        A point aimed at a thin slider is given some room."""
        for kind, class_name, room in (
            ("wheel", "XCUIElementTypePickerWheel", 0.0),
            ("slider", "XCUIElementTypeSlider", SLIDER_ROOM),
        ):
            found = await self._session_call(
                "POST", "/elements", {"using": "class name", "value": class_name}
            )
            for element in found if isinstance(found, list) else []:
                element_id = (
                    next(iter(element.values()), None) if isinstance(element, dict) else None
                )
                if not element_id:
                    continue
                rect = await self._session_call("GET", f"/element/{element_id}/rect")
                try:
                    left, top = float(rect["x"]), float(rect["y"])
                    right, bottom = left + float(rect["width"]), top + float(rect["height"])
                except (KeyError, TypeError, ValueError):
                    continue
                if left - room <= x < right + room and top - room <= y < bottom + room:
                    return kind, str(element_id)
        raise DeviceError("no_picker")

    async def check_picker_target(self, x: float, y: float) -> None:
        await self._control_at(x, y)

    async def set_picker(self, x: float, y: float, value: str) -> None:
        # XCUITest scrolls the wheel to the item. A value the wheel does not offer leaves
        # it unchanged; the next screenshot shows which.
        kind, element_id = await self._control_at(x, y)
        if kind == "slider":
            await self._set_slider(element_id, value)
            return
        await self._session_call("POST", f"/element/{element_id}/value", {"text": value})

    async def _set_slider(self, element_id: str, value: str) -> None:
        """Sets a slider to the position it reports as ``value`` ("27%"). Dragging to a
        point lands on a neighbouring step about every other time, and so does XCUITest's own
        setting, but it reports where it came to rest: try again until the slider reports the
        value, a few times at most. A value that is not a percentage leaves it unchanged."""
        percent = parse_percent(value)
        if percent is None:
            return
        for offset in SLIDER_OFFSETS[:SLIDER_TRIES]:
            # In the middle of the step's share of the range, away from the rounding edge.
            position = round(min(1.0, (percent + offset) / 100), 4)
            await self._session_call(
                "POST", f"/element/{element_id}/value", {"text": str(position)}
            )
            reported = await self._session_call("GET", f"/element/{element_id}/attribute/value")
            if parse_percent(str(reported or "")) == percent:
                return

    async def back(self) -> None:
        raise DeviceError("unsupported")

    @property
    def capabilities(self) -> list[Capability]:
        """A simulator has no volume keys: XCUITest presses them on a real iPhone only."""
        if SIMULATOR_UDID.match(self.native_id):
            return list(self._capabilities)
        return [*self._capabilities, "volume_up", "volume_down"]

    async def home(self) -> None:
        await self._script("mobile: pressButton", {"name": "home"})

    async def volume_up(self) -> None:
        await self._script("mobile: pressButton", {"name": "volumeUp"})

    async def volume_down(self) -> None:
        await self._script("mobile: pressButton", {"name": "volumeDown"})

    async def check_app(self, app_id: str) -> None:
        if await self._script("mobile: isAppInstalled", {"bundleId": app_id}) is not True:
            raise DeviceError("app_not_installed")

    async def launch_app(self, app_id: str) -> None:
        await self._script("mobile: activateApp", {"bundleId": app_id})

    async def list_apps(self) -> dict[str, str | None]:
        if SIMULATOR_UDID.match(self.native_id):
            apps = await _simulator_apps(self.native_id)
        else:
            apps = {}
            for kind in ("User", "System"):
                found = await self._script(
                    "mobile: listApps",
                    {
                        "applicationType": kind,
                        "returnAttributes": ["CFBundleDisplayName", "CFBundleName"],
                    },
                )
                apps.update(found if isinstance(found, dict) else {})
        return {str(app_id): _names(info) for app_id, info in apps.items()}


def _names(info: Any) -> str | None:
    """The names an app goes by: the one under its icon, and its bundle name when that
    differs (Xiaohongshu shows "rednote" on an English iPhone)."""
    found = (info or {}).get("CFBundleDisplayName"), (info or {}).get("CFBundleName")
    names = list(dict.fromkeys(str(n) for n in found if n))
    return " · ".join(names) or None


async def _devicectl(udid: str, *command: str) -> dict[str, Any] | None:
    """What a real iPhone answers to ``xcrun devicectl <command>`` (outside XCTest, a
    fraction of a second), or None when it cannot be asked or the command failed."""
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "result.json"
        try:
            process = await asyncio.create_subprocess_exec(
                "xcrun",
                "devicectl",
                *command,
                "--device",
                udid,
                "--json-output",
                str(out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await asyncio.wait_for(process.wait(), 10) != 0:
                return None
            result = json.loads(out.read_text()).get("result")
        except (OSError, TimeoutError, ValueError):
            return None
    return result if isinstance(result, dict) else {}


async def passcode_required(udid: str) -> bool | None:
    """Whether a real iPhone is locked and needs its passcode now, as the phone itself says;
    None when it cannot be told."""
    result = await _devicectl(udid, "device", "info", "lockState") or {}
    required = result.get("passcodeRequired")
    return required if isinstance(required, bool) else None


async def stop_agent(udid: str) -> bool:
    """Stops WebDriverAgent's process on a real iPhone and leaves its app installed: the next
    session's xcodebuild starts that app again. Whether it was stopped (or not running).
    Appium's own way, useNewWDA, uninstalls the app first: with it went the last app of a
    free developer team, and with that the phone's trust in the team's certificate, so the
    new agent was refused until someone trusted the certificate again in Settings."""
    listing = await _devicectl(udid, "device", "info", "processes")
    if listing is None:
        return False
    stopped = True
    for process in listing.get("runningProcesses") or []:
        if AGENT_APP in str(process.get("executable", "")):
            pid = str(process.get("processIdentifier"))
            killed = await _devicectl(
                udid, "device", "process", "terminate", "--pid", pid, "--kill"
            )
            stopped = stopped and killed is not None
    return stopped


async def _host_pasteboard(udid: str, text: str) -> bool:
    """Puts ``text`` on a phone's pasteboard from this Mac and reads it back: whether the
    phone now holds it. simctl for a simulator, devicectl for a real iPhone (iOS 17 or
    later). WebDriverAgent's own way (Appium's set_clipboard) did nothing on a real iPhone,
    where its app runs in the background, and answered that it had worked: Paste then put
    in what was there before, a picture copied on the Mac."""
    if SIMULATOR_UDID.match(udid):
        copy, paste = ["simctl", "pbcopy", udid], ["simctl", "pbpaste", udid]
    else:
        device = ["--device", udid, "--quiet"]
        copy = ["devicectl", "device", "pasteboard", "copy", *device]
        paste = ["devicectl", "device", "pasteboard", "paste", *device]
    return await _xcrun(copy, text.encode()) is not None and await _xcrun(paste) == text.encode()


async def _xcrun(command: list[str], data: bytes = b"") -> bytes | None:
    """What ``xcrun <command>`` writes, given ``data``; None when it failed."""
    try:
        process = await asyncio.create_subprocess_exec(
            "xcrun",
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(data), 10)
    except (OSError, TimeoutError):
        return None
    return out if process.returncode == 0 else None


async def _simulator_apps(udid: str) -> dict[str, Any]:
    """Apps on a simulator, from simctl (Appium lists apps on real devices only). The
    simulator runs on this Mac, next to the Gateway."""
    try:
        listing = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "listapps", udid, stdout=asyncio.subprocess.PIPE
        )
        plist, _ = await asyncio.wait_for(listing.communicate(), 30)
        convert = await asyncio.create_subprocess_exec(
            "plutil",
            "-convert",
            "json",
            "-o",
            "-",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        data, _ = await asyncio.wait_for(convert.communicate(plist), 30)
        apps = json.loads(data)
    except (OSError, TimeoutError, ValueError):
        raise DeviceError("app_list_unavailable") from None
    if not isinstance(apps, dict):
        raise DeviceError("app_list_unavailable")
    return apps
