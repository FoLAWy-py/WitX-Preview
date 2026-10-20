"""Finding an option on the screen by its text: strict, so that what is tapped is
what was asked for."""

import pytest
from witx_gateway.options import Option, find_option, normalize, parse_options, signature

SCREEN = (1280.0, 2856.0)


def node(tag: str, **attrs: str) -> str:
    return f"<{tag} " + " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items()) + "/>"


FLAG_CN, FLAG_US, FLAG_HK = "&#127464;&#127475;", "&#127482;&#127480;", "&#127469;&#127472;"

# The country list of the Android Contacts app, as UiAutomator2 prints it.
ANDROID = (
    "<hierarchy>"
    + "".join(
        [
            node("ScrollView", text="", bounds="[144,324][1136,2102]", displayed="true"),
            node(
                "TextView",
                text=f"{FLAG_US} United States +1",
                bounds="[180,366][560,426]",
                displayed="true",
            ),
            node(
                "TextView",
                text=f"{FLAG_CN} China +86",
                bounds="[180,900][500,960]",
                displayed="true",
            ),
            node(
                "TextView",
                text=f"{FLAG_HK} Hong Kong SAR China +852",
                bounds="[180,1000][700,1060]",
                displayed="true",
            ),
            node("TextView", text="Hidden +9", bounds="[180,1100][500,1160]", displayed="false"),
            node("TextView", text="Off screen +7", bounds="[180,3000][500,3060]", displayed="true"),
        ]
    )
    + "</hierarchy>"
)

IOS = (
    "<AppiumAUT>"
    + "".join(
        [
            node(
                "XCUIElementTypeStaticText",
                label="Germany",
                name="Germany",
                visible="true",
                x="40",
                y="300",
                width="120",
                height="22",
            ),
            node(
                "XCUIElementTypeStaticText",
                label="Greece",
                name="Greece",
                visible="false",
                x="40",
                y="340",
                width="120",
                height="22",
            ),
        ]
    )
    + "</AppiumAUT>"
)


def test_the_options_on_the_screen_are_read_from_the_android_ui_tree() -> None:
    options = parse_options(ANDROID)
    assert [o.text for o in options][:3] == [
        "\U0001f1fa\U0001f1f8 United States +1",
        "\U0001f1e8\U0001f1f3 China +86",
        "\U0001f1ed\U0001f1f0 Hong Kong SAR China +852",
    ]
    assert "Hidden +9" not in [o.text for o in options]  # not displayed
    assert options[1].box == (180.0, 900.0, 320.0, 60.0)


def test_the_options_on_the_screen_are_read_from_the_ios_ui_tree() -> None:
    assert [(o.text, o.box) for o in parse_options(IOS)] == [
        ("Germany", (40.0, 300.0, 120.0, 22.0))
    ]


def test_a_broken_ui_tree_has_no_options() -> None:
    assert parse_options("<not xml") == []


def test_the_flag_in_front_of_an_option_is_not_part_of_what_it_shows() -> None:
    assert normalize("\U0001f1e8\U0001f1f3 China +86") == normalize("china  +86") == "china +86"


def test_exactly_one_option_showing_the_text_is_found() -> None:
    verdict, option = find_option(parse_options(ANDROID), "China +86", SCREEN)
    assert verdict == "found" and option is not None and option.center == (340.0, 930.0)


def test_text_that_only_contains_the_wanted_text_is_not_a_match() -> None:
    # "China" is in "Hong Kong SAR China +852" and in "China +86": neither is "China".
    assert find_option(parse_options(ANDROID), "China", SCREEN) == ("missing", None)
    assert find_option(parse_options(ANDROID), "China +8", SCREEN) == ("missing", None)


def test_an_option_that_is_not_on_the_screen_is_not_found() -> None:
    assert find_option(parse_options(ANDROID), "Off screen +7", SCREEN) == ("missing", None)
    assert find_option(parse_options(ANDROID), "Hidden +9", SCREEN) == ("missing", None)


def test_two_options_showing_the_same_text_are_ambiguous() -> None:
    twice = [Option("Home", (10, 100, 200, 40)), Option("Home", (10, 600, 200, 40))]
    assert find_option(twice, "home", SCREEN) == ("ambiguous", None)


def test_an_item_and_the_label_inside_it_are_one_option() -> None:
    nested = [Option("China +86", (0, 900, 1000, 80)), Option("China +86", (180, 910, 300, 60))]
    verdict, option = find_option(nested, "China +86", SCREEN)
    assert verdict == "found" and option is not None
    assert option.box == (180, 910, 300, 60)  # the smaller one is tapped


@pytest.mark.parametrize("moved", [False, True])
def test_a_swipe_that_moved_the_list_changes_what_is_on_the_screen(moved: bool) -> None:
    before = parse_options(ANDROID)
    after = before[1:] if moved else list(before)
    assert (signature(after) != signature(before)) is moved
