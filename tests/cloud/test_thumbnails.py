import io

import pytest
from PIL import Image
from witx.services.thumbnails import MAX_PIXELS, NotAThumbnail, thumbnail


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(out, "PNG")
    return out.getvalue()


def test_a_thumbnail_is_a_jpeg_of_the_asked_width_in_the_same_proportions() -> None:
    small = Image.open(io.BytesIO(thumbnail(_png(1080, 2400), 240)))
    assert small.format == "JPEG" and small.size == (240, 533)


def test_a_thumbnail_is_never_wider_than_the_screenshot() -> None:
    assert Image.open(io.BytesIO(thumbnail(_png(200, 400), 600))).size == (200, 400)


def test_a_picture_that_cannot_be_made_small_is_refused() -> None:
    with pytest.raises(NotAThumbnail):
        thumbnail(b"not a picture", 240)
    side = int(MAX_PIXELS**0.5) + 1
    with pytest.raises(NotAThumbnail):
        thumbnail(_png(side, side), 240)


def test_the_transparent_parts_of_a_thumbnail_are_white() -> None:
    out = io.BytesIO()
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(out, "PNG")
    small = Image.open(io.BytesIO(thumbnail(out.getvalue(), 20)))
    assert small.getpixel((10, 10)) == (255, 255, 255)
