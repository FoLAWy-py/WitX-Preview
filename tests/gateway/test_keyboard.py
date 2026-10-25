"""Typing by tapping the keyboard and pasting through a field's menu: the plan from
the page source, and the iOS adapter against a keyboard simulated behind a mock Appium."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import witx_gateway.devices.ios as ios_module
from witx_gateway import keyboard
from witx_gateway.devices.base import TypeMethod
from witx_gateway.devices.ios import IOSDevice

from tests.gateway.test_devices import SIMULATOR, Requests, appium

# ---------------------------------------------------------------- a keyboard on a screen

WIDTH = 440
BAR_Y = 600  # the candidate bar, above the keys
KEYS_Y = 650
ROW = 56
LETTERS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
NUMBERS = ["1234567890", "，。！“”"]
# Pinyin and the candidates its letters bring up, best first: (text, letters it takes).
WORDS: dict[str, list[tuple[str, str]]] = {
    "nihao": [("你好", "nihao"), ("你", "ni")],
    "nihaoshijie": [("你好世界", "nihaoshijie"), ("你好", "nihao"), ("你", "ni")],
    "shijie": [("世界", "shijie"), ("是", "shi")],
    "xiangaohedan": [("线稿核弹", "xiangaohedan"), ("想", "xiang"), ("线稿", "xiangao")],
    "xiangao": [("线稿", "xiangao"), ("想", "xiang")],
    "hedan": [("核弹", "hedan"), ("和单", "hedan")],
    "he": [("和", "he")],
    "dan": [("淡", "dan"), ("单", "dan")],
    "zhu": [("住", "zhu"), ("主", "zhu"), ("逐", "zhu")],
}
AUTOCORRECT = {"teh": "the"}
FIELD = (20, 300, 300, 44)  # the text box, above the keyboard


@dataclass
class Phone:
    """A text box with a pinyin keyboard and a Latin one, as WebDriverAgent shows them."""

    kind: str = "pinyin"  # or "latin"
    page: str = "letters"  # or "numbers"
    composing: str = ""
    text: str = ""  # what the field holds
    shift: bool = False
    kept: bool = False  # the last Latin word was tapped as typed
    keyboards: tuple[str, ...] = ("pinyin", "latin")
    # Candidates beyond the bar's edge, each for the letters being composed.
    hidden: set[tuple[str, str]] = field(default_factory=set)
    sent: bool = False
    taps: list[tuple[float, float]] = field(default_factory=list)
    pasteboard: str = ""
    pasteboard_writes: bool = True  # False: writes to it do nothing, and say they worked
    takes_paste: bool = True  # False: a paste puts no text in (a picture on the pasteboard)
    secure: bool = False  # the focused field is a password field
    focused: bool = True  # False: no field has the focus (it closed)
    menu: tuple[str, ...] = ("Paste", "Select")  # the field's edit menu, as on iOS 27.2
    # An earlier menu, hidden, that the page source still holds before the one in view.
    stale_menu: tuple[str, ...] = ("Cut", "Copy", "Paste")
    menu_open: bool = False  # a long press on the field opens its menu; a tap toggles it
    cursor: int | None = None  # where in the text the cursor is; None: at its end
    opened: bool = False  # the candidates are opened over the keys
    # Candidates picked while the rest is still composed: a delete puts the last back.
    picked: list[tuple[str, str]] = field(default_factory=list)

    # -- what the screen shows

    def keys(self) -> dict[str, tuple[float, float, float, float]]:
        rows = LETTERS if self.page == "letters" else NUMBERS
        boxes: dict[str, tuple[float, float, float, float]] = {}
        for r, row in enumerate(rows):
            for c, char in enumerate(row):
                name = char.upper() if self.shift and char.isalpha() else char
                boxes[name] = (4 + (c + (r == 2)) * 43, KEYS_Y + r * ROW, 40, 50)
        bottom = KEYS_Y + 3 * ROW
        return boxes | {
            "shift": (4, KEYS_Y + 2 * ROW, 40, 50),
            "delete": (380, KEYS_Y + 2 * ROW, 56, 50),
            "more": (4, bottom, 50, 50),
            "space": (112, bottom, 200, 50),
            "Go": (360, bottom, 76, 50),
            "Next keyboard": (2, bottom + ROW, 80, 50),
        }

    def labels(self) -> dict[str, str]:
        chinese = self.kind == "pinyin"
        return {
            "delete": "删除" if chinese else "delete",
            "more": ("数字" if self.page == "letters" else "字母") if chinese else "numbers",
        }

    def candidates(self) -> list[tuple[str, str]]:
        if self.kind != "pinyin" or not self.composing:
            return []
        return WORDS.get(self.composing, [(self.composing, self.composing)])

    def predictions(self) -> list[str]:
        word = self.text.rsplit(" ", 1)[-1]
        return [f"“{word}”"] if self.kind == "latin" and word in AUTOCORRECT else []

    def grid(self, n: int) -> tuple[float, float]:
        """Where the nth candidate is once they are opened over the keys."""
        return (n % 6) * 66, KEYS_Y + (n // 6) * 50

    def source(self) -> str:
        cells, x = [], 0
        for n, (text, _) in enumerate(self.candidates()):
            hidden = (self.composing, text) in self.hidden
            at, y = self.grid(n) if self.opened else (WIDTH + 10 if hidden else x, BAR_Y)
            cells.append(
                f'<XCUIElementTypeCell type="XCUIElementTypeCell" label="{text}, 说明" '
                f'x="{at}" y="{y}" width="60" height="44"/>'
            )
            x += 0 if hidden else 62
        if self.candidates():
            button = "Close more suggestions" if self.opened else "More suggestions"
            cells.append(
                f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{button}" '
                f'label="{button}" x="395" y="{BAR_Y}" width="45" height="44"/>'
            )
        for word in self.predictions():
            cells.append(
                f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{word}" '
                f'label="{word}" x="0" y="{BAR_Y}" width="140" height="44"/>'
            )
        labels = self.labels()
        keys = "".join(
            f'<XCUIElementTypeKey type="XCUIElementTypeKey" name="{name}" '
            f'label="{labels.get(name, name)}" x="{b[0]}" y="{b[1]}" width="{b[2]}" '
            f'height="{b[3]}"/>'
            for name, b in self.keys().items()
            if not self.opened or name == "Next keyboard"
        )
        menu = "".join(
            f'<XCUIElementTypeMenuItem type="XCUIElementTypeMenuItem" label="{item}" '
            f'x="{x}" y="500" width="70" height="44"/>'
            for item, x, _ in self.menu_items()
        )
        return (
            '<XCUIElementTypeApplication type="XCUIElementTypeApplication">'
            '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="440" '
            'height="956">'
            # The app's own list above the keyboard shows a candidate's text too.
            '<XCUIElementTypeCell type="XCUIElementTypeCell" label="你好, 说明" x="0" '
            'y="100" width="200" height="44"/>'
            f"{menu}</XCUIElementTypeWindow>"
            '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="440" '
            'height="956"><XCUIElementTypeOther type="XCUIElementTypeOther">'
            f"{''.join(cells)}</XCUIElementTypeOther>"
            f'<XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" x="0" y="{KEYS_Y}" '
            f'width="{WIDTH}" height="306">{keys}</XCUIElementTypeKeyboard>'
            "</XCUIElementTypeWindow></XCUIElementTypeApplication>"
        )

    def menu_items(self) -> list[tuple[str, float, bool]]:
        """The menu items the page source holds: label, x, and whether it is in view."""
        stale = [(item, 20 + 80 * n, False) for n, item in enumerate(self.stale_menu)]
        return stale + [(item, 20 + 80 * n, self.menu_open) for n, item in enumerate(self.menu)]

    # -- what a tap does

    def tap(self, x: float, y: float, hold: int = 0) -> None:
        self.taps.append((x, y))
        for item, n in zip(self.menu, range(len(self.menu)), strict=True):
            if self.menu_open and 20 + 80 * n <= x <= 90 + 80 * n and 500 <= y <= 544:
                if item == "Paste" and self.takes_paste:
                    self.text += self.pasteboard
                self.menu_open = False
                return
        fx, fy, fw, fh = FIELD
        if fx <= x <= fx + fw and fy <= y <= fy + fh:  # the cursor goes where it is tapped
            self.cursor = None if x >= fx + fw * 0.8 else 0
            self.menu_open = hold >= 500 or not self.menu_open
            return
        if self.candidates() and 395 <= x <= 440 and BAR_Y <= y <= BAR_Y + 44:
            self.opened = not self.opened
            return
        if self.opened:
            for n, (text, letters) in enumerate(self.candidates()):
                gx, gy = self.grid(n)
                if gx <= x <= gx + 60 and gy <= y <= gy + 44:
                    self.pick(text, letters)
                    self.opened = bool(self.composing)  # open until the word is picked
            return
        cx = 0
        for text, letters in self.candidates():
            if (self.composing, text) in self.hidden:
                continue
            if cx <= x <= cx + 60 and BAR_Y <= y <= BAR_Y + 44:
                self.pick(text, letters)
                return
            cx += 62
        if self.predictions() and 0 <= x <= 140 and BAR_Y <= y <= BAR_Y + 44:
            self.text += " "
            self.kept = True
            return
        for name, (bx, by, bw, bh) in self.keys().items():
            if bx <= x <= bx + bw and by <= y <= by + bh:
                for _ in range(max(1, hold // 100) if name == "delete" else 1):
                    self.press(name)  # a held delete key repeats
                return

    def pick(self, text: str, letters: str) -> None:
        self.text += text
        self.composing = self.composing[len(letters) :]
        self.picked = [*self.picked, (text, letters)] if self.composing else []

    def press(self, name: str) -> None:
        if name == "Go":
            self.sent = True
        elif name == "Next keyboard":
            self.kind = self.keyboards[(self.keyboards.index(self.kind) + 1) % 2]
            self.page, self.composing = "letters", ""
        elif name == "shift":
            self.shift = not self.shift
        elif name == "more":
            self.page = "numbers" if self.page == "letters" else "letters"
        elif name == "delete" and self.picked:
            text, letters = self.picked.pop()
            self.text, self.composing = self.text[: -len(text)], letters + self.composing
        elif name == "delete":
            at = len(self.text) if self.cursor is None else self.cursor
            if self.composing:
                self.composing = self.composing[:-1]
            elif at:
                self.text = self.text[: at - 1] + self.text[at:]
                self.cursor = None if self.cursor is None else at - 1
        elif name == "space":
            word = self.text.rsplit(" ", 1)[-1]
            if self.kind == "latin" and word in AUTOCORRECT and not self.kept:
                self.text = self.text[: -len(word)] + AUTOCORRECT[word]
            self.text += " "
            self.kept = False
        elif len(name) == 1 and name.isalpha():
            if self.kind == "pinyin":
                self.composing += name
            else:
                self.text += name
            self.shift = False
        else:
            self.text += name
            if name in "，。":
                self.page = "letters"  # a pinyin keyboard turns back after these

    def on_actions(self, body: Any) -> None:
        starts = []
        for source in body["actions"]:
            steps = source["actions"]
            start = steps[0]["duration"] if steps[0]["type"] == "pause" else 0
            move = next(s for s in steps if s["type"] == "pointerMove")
            hold = sum(s["duration"] for s in steps[steps.index(move) :] if s["type"] == "pause")
            starts.append((start, move["x"], move["y"], hold))
        for _, x, y, hold in sorted(starts):
            self.tap(x, y, hold)

    def xcrun(self, command: list[str], data: bytes = b"") -> bytes | None:
        """The Mac's simctl, writing and reading this phone's pasteboard."""
        if command[:2] == ["simctl", "pbcopy"]:
            if self.pasteboard_writes:
                self.pasteboard = data.decode()
            return b""
        if command[:2] == ["simctl", "pbpaste"]:
            return self.pasteboard.encode()
        return None


def phone_device(phone: Phone, monkeypatch: pytest.MonkeyPatch) -> IOSDevice:
    monkeypatch.setattr(ios_module, "BOARD_SETTLE", 0)
    monkeypatch.setattr(ios_module, "MENU_SETTLE", 0)
    monkeypatch.setattr(ios_module, "PASTE_SETTLE", 0)

    async def xcrun(command: list[str], data: bytes = b"") -> bytes | None:
        return phone.xcrun(command, data)

    monkeypatch.setattr(ios_module, "_xcrun", xcrun)
    requests: Requests = []
    world: dict[str, Any] = {
        "device_info": {"isSimulator": True},
        "on_actions": phone.on_actions,
        "field_rect": dict(zip(("x", "y", "width", "height"), FIELD, strict=True)),
    }
    base = appium(requests, world)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        ok = lambda value: httpx.Response(200, json={"value": value})  # noqa: E731
        if path.endswith("/execute/sync") and body["script"] == "mobile: source":
            return ok(phone.source())
        if path.endswith("/element/active") and not phone.focused:
            return httpx.Response(404, json={"value": {"error": "no such element"}})
        if path.endswith("/element/E1/attribute/type") and phone.secure:
            return ok("XCUIElementTypeSecureTextField")
        if path.endswith("/elements") and body["using"] == "-ios class chain":
            shown = [n for n, (_, _, seen) in enumerate(phone.menu_items()) if seen]
            if body["value"] == ios_module.PASTE_ITEM:
                shown = [n for n in shown if phone.menu_items()[n][0] == "Paste"]
            else:
                assert body["value"] == ios_module.MENU_ITEM
            return ok([{"ELEMENT": f"M{n}"} for n in shown])
        if "/element/M" in path and path.endswith("/rect"):
            _, x, _ = phone.menu_items()[int(path.split("/element/M")[1].split("/")[0])]
            return ok({"x": x, "y": 500, "width": 70, "height": 44})
        if path.endswith("/element/E1/attribute/value"):
            return ok(phone.text or "说点什么...")
        if path.endswith("/element/E1/attribute/placeholderValue"):
            return ok("说点什么...")
        return base.handler(request)  # type: ignore[no-any-return]

    return IOSDevice(
        udid=SIMULATOR,
        name="iPhone",
        appium_url="http://127.0.0.1:4723",
        transport=httpx.MockTransport(handler),
    )


def kept_inside_the_keyboard(phone: Phone) -> bool:
    """Every tap landed on the keyboard's keys or its candidate bar."""
    return all(y >= BAR_Y and 0 <= x <= WIDTH for x, y in phone.taps)


# ---------------------------------------------------------------- the plan


def test_only_the_keyboard_s_window_and_candidates_in_view_count() -> None:
    board = keyboard.parse_board(Phone(composing="nihao", hidden={("nihao", "你")}).source())
    assert board is not None
    assert [text for text, _ in board.candidates] == ["你好"]  # not the app's, nor one off it
    assert board.chinese and board.letters and "Go" in board.keys


def test_a_word_the_bar_offers_is_never_taken_for_the_key_it_is_named_like() -> None:
    phone = Phone(kind="latin", shift=True, text="teh")  # "I" is offered at a sentence's start
    source = phone.source().replace(
        "</XCUIElementTypeOther>",
        '<XCUIElementTypeButton type="XCUIElementTypeButton" name="I" label="I" x="150" '
        'y="600" width="140" height="44"/></XCUIElementTypeOther>',
    )
    board = keyboard.parse_board(source)
    assert board is not None and board.keys["I"][1] == KEYS_Y and "I" in board.offered


def test_text_is_split_into_chinese_latin_and_single_characters() -> None:
    assert [(t.kind, t.text) for t in keyboard.tokens("WitX 测试，订单12")] == [
        ("latin", "WitX"),
        ("char", " "),
        ("han", "测试"),
        ("char", "，"),
        ("han", "订单"),
        ("char", "1"),
        ("char", "2"),
    ]


def test_pinyin_is_read_word_by_word_with_v_for_u_umlaut() -> None:
    assert keyboard.syllables("银行绿色") == ["yin", "hang", "lv", "se"]


def test_the_candidate_putting_in_the_most_from_the_start_is_picked() -> None:
    shown = (("你", (0, 0, 1, 1)), ("你好", (2, 0, 1, 1)), ("好", (4, 0, 1, 1)))
    assert keyboard.pick("你好世界", shown) == ("你好", (2, 0, 1, 1))
    assert keyboard.pick("世界", shown) is None
    gaps = (("ni\u2006hao", (0, 0, 1, 1)),)
    assert keyboard.pick_as_typed("nihao", gaps) == gaps[0]


def test_a_new_line_or_an_emoji_cannot_be_tapped_in() -> None:
    assert keyboard.untypable("第一行\n第二行") == "\n"
    assert keyboard.untypable("好\U0001f600") == "\U0001f600"
    assert keyboard.untypable("你好，WitX 123！") is None


# ---------------------------------------------------------------- tapping on the phone


async def test_chinese_goes_in_by_the_candidates_that_show_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone()
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("你好世界", method="keys") is None
    assert phone.text == "你好世界"
    assert kept_inside_the_keyboard(phone) and not phone.sent


async def test_a_candidate_beyond_the_bar_s_edge_is_found_with_the_bar_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(hidden={("xiangaohedan", "线稿")})  # the bar shows 线稿核弹 and 想
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("线稿和淡", method="keys") is None
    assert phone.text == "线稿和淡" and phone.composing == "" and not phone.opened


async def test_a_piece_no_candidate_shows_is_taken_back_whole_and_typed_in_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After 线稿, "hedan" offers nothing starting 和; a delete then puts 线稿 back to pinyin.
    phone = Phone()
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("线稿和淡", method="keys") is None
    assert phone.text == "线稿和淡" and phone.composing == ""


async def test_a_rare_character_is_found_with_the_candidates_opened_over_the_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(hidden={("zhu", "逐")})  # the bar shows 住 and 主
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("逐", method="keys") is None
    assert phone.text == "逐" and not phone.opened


async def test_a_candidate_that_never_shows_stops_typing_and_takes_its_letters_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone()  # "hao" brings up no candidate showing 好, not even opened
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("好", method="keys") == "no_candidate"
    assert phone.text == "" and phone.composing == "" and not phone.opened


async def test_a_mark_after_which_the_keyboard_turns_back_by_itself_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone()
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("你好，世界1！", method="keys") is None
    assert phone.text == "你好，世界1！"


async def test_latin_words_are_kept_as_typed_on_a_latin_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone()
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("teh 你好", method="keys") is None
    assert phone.text == "teh 你好"  # autocorrection would have made it "the"
    assert kept_inside_the_keyboard(phone) and not phone.sent


async def test_chinese_is_not_tapped_in_without_a_pinyin_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(kind="latin", keyboards=("latin", "latin"))
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("你好", method="keys") == "no_pinyin_keyboard"
    assert phone.text == "" and not phone.sent


async def test_replacing_by_keys_deletes_all_the_field_held_wherever_the_cursor_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(text="旧的", cursor=0)  # deleting from the cursor alone leaves it all
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("你好", replace=True, method="keys") is None
    assert phone.text == "你好"


async def test_a_long_text_is_deleted_in_rounds_of_the_held_delete_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(text="旧" * 120)
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("你好", replace=True, method="keys") is None
    assert phone.text == "你好" and not phone.sent


# ---------------------------------------------------------------- pasting


@pytest.mark.parametrize("method", ["keys", "paste"])
async def test_nothing_is_tapped_into_a_password_field_the_focus_moved_to_after_the_check(
    method: TypeMethod, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone = Phone()
    device = phone_device(phone, monkeypatch)
    await device.check_text_target()
    phone.secure = True  # the form moved on to its password field
    assert await device.type_text("hunter2", method=method) == "secure_field"
    assert (phone.text, phone.pasteboard, phone.taps) == ("", "", [])


@pytest.mark.parametrize("method", ["keys", "paste"])
async def test_nothing_is_tapped_or_pasted_when_the_field_is_gone_before_the_first_key(
    method: TypeMethod, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone = Phone(kind="latin")  # Chinese would first turn to the pinyin keyboard
    device = phone_device(phone, monkeypatch)
    await device.check_text_target()
    phone.focused = False  # the field closed after the check
    assert await device.type_text("你好", replace=True, method=method) == "no_focused_field"
    assert (phone.kind, phone.text, phone.pasteboard, phone.taps) == ("latin", "", "", [])


async def test_a_paste_goes_in_through_the_menu_and_leaves_the_pasteboard_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone()
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("粘贴：WitX，123！", method="paste") is None
    assert phone.text == "粘贴：WitX，123！" and phone.pasteboard == ""


async def test_the_pasteboard_is_emptied_also_when_no_paste_menu_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(menu=())
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("秘密的文字", method="paste") == "no_paste_menu"
    assert phone.text == "" and phone.pasteboard == ""


async def test_a_paste_into_a_field_with_text_replaces_it_or_is_not_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(text="旧的", cursor=0)
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("新的", method="paste") == "field_not_empty"
    assert phone.text == "旧的"
    assert await device.type_text("新的", replace=True, method="paste") is None
    assert phone.text == "新的"


async def test_a_pasteboard_that_does_not_take_the_text_stops_the_paste_before_the_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(pasteboard="一张图片", pasteboard_writes=False)
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("新的", method="paste") == "pasteboard_not_set"
    assert phone.text == "" and phone.taps == []


async def test_a_paste_the_field_does_not_take_is_reported_and_the_pasteboard_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(takes_paste=False)
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("秘密的文字", method="paste") == "paste_not_taken"
    assert phone.pasteboard == ""


async def test_a_real_iphone_s_pasteboard_is_set_by_devicectl_and_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"text": b"a picture"}
    commands: list[list[str]] = []

    async def xcrun(command: list[str], data: bytes = b"") -> bytes | None:
        commands.append(command)
        if command[:3] != ["devicectl", "device", "pasteboard"]:
            return None
        # A copy says it worked, and the pasteboard may still hold what it held.
        return b"" if command[3] == "copy" else held["text"]

    monkeypatch.setattr(ios_module, "_xcrun", xcrun)
    assert not await ios_module._host_pasteboard("00000000-0000000000000001", "新的")
    assert [c[3] for c in commands] == ["copy", "paste"]
    assert all(c[c.index("--device") + 1] == "00000000-0000000000000001" for c in commands)
    held["text"] = "新的".encode()
    assert await ios_module._host_pasteboard("00000000-0000000000000001", "新的")


async def test_a_paste_left_hidden_from_an_earlier_menu_is_never_tapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(stale_menu=("Cut", "Copy", "Paste"), menu=("Select", "Paste"))
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("新的", method="paste") is None
    assert phone.text == "新的"
    assert all(not 180 <= x <= 250 or y != 522 for x, y in phone.taps)  # the hidden Paste


async def test_an_edit_menu_typing_opened_is_not_left_in_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = Phone(menu=("Select", "AutoFill"))  # it offers no Paste
    device = phone_device(phone, monkeypatch)
    assert await device.type_text("新的", method="paste") == "no_paste_menu"
    assert not phone.menu_open
    phone.text, phone.menu = "旧的", ("Paste", "Select")
    assert await device.type_text("你好", replace=True, method="keys") is None
    assert phone.text == "你好" and not phone.menu_open
