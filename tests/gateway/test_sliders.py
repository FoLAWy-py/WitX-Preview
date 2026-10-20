"""Reading the position of the sliders on an Android screen."""

import pytest
from witx_gateway.sliders import parse_android_sliders, parse_percent


def node(tag: str, children: str = "", **attrs: str) -> str:
    body = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f"<{tag} {body}>{children}</{tag}>"


def seek_bar(name: str, step: str, bounds: str, *, thumb_shown: str = "true") -> str:
    # The Settings slider as UiAutomator2 prints it once hidden views are allowed: the bar
    # is not "displayed", it holds one node for its thumb, and that node's text is the step.
    thumb = node(
        "android.widget.SeekBar",
        text=step,
        content_desc=f"{name},",
        bounds="[345,1770][489,1914]",
        displayed=thumb_shown,
    )
    return node(
        "android.widget.SeekBar",
        thumb,
        text="",
        content_desc=name,
        bounds=bounds,
        displayed="false",
    )


def page(*parts: str) -> str:
    return "<hierarchy>" + "".join(parts) + "</hierarchy>"


def test_a_slider_is_read_as_its_name_and_the_step_its_thumb_reports() -> None:
    (font,) = parse_android_sliders(page(seek_bar("Font size", "1.0", "[240,1770][1040,1914]")))
    assert (font.name, font.value) == ("Font size", "1.0")
    assert font.center == (640.0, 1842.0)


def test_sliders_come_top_to_bottom() -> None:
    source = page(
        seek_bar("Display size", "2.0", "[240,2138][1040,2282]"),
        seek_bar("Font size", "1.0", "[240,1770][1040,1914]"),
    )
    assert [s.name for s in parse_android_sliders(source)] == ["Font size", "Display size"]


def test_a_slider_out_of_view_is_left_out() -> None:
    source = page(seek_bar("Font size", "1.0", "[240,3000][1040,3144]", thumb_shown="false"))
    assert parse_android_sliders(source) == []


def test_a_bar_that_carries_its_own_position_is_read() -> None:
    source = page(
        node(
            "android.widget.SeekBar",
            text="40",
            content_desc="Brightness",
            bounds="[100,500][900,600]",
            displayed="true",
        )
    )
    assert [(s.name, s.value) for s in parse_android_sliders(source)] == [("Brightness", "40")]


def test_a_bar_that_reports_no_position_is_left_out() -> None:
    source = page(node("android.widget.SeekBar", text="", bounds="[0,0][10,10]", displayed="true"))
    assert parse_android_sliders(source) == []


def test_an_unreadable_page_has_no_sliders() -> None:
    assert parse_android_sliders("not xml") == []


@pytest.mark.parametrize(
    ("text", "percent"),
    [
        ("27%", 27),
        (" 9 % ", 9),
        ("100", 100),
        ("0%", 0),
        ("101%", None),
        ("1.0", None),
        ("large", None),
    ],
)
def test_a_percentage_is_read_from_what_a_slider_reports(text: str, percent: int | None) -> None:
    assert parse_percent(text) == percent
