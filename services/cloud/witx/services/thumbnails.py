"""Small copies of screenshots for the conversation, where a phone's screen is shown a
few dozen pixels wide: the page asks for a width and gets a JPEG of it, not the PNG."""

import io

from PIL import Image

QUALITY = 80
# A phone screen is at most a few million pixels. A picture far larger is not decoded
# (it would take gigabytes): the caller sends the stored one as it is instead.
MAX_PIXELS = 16_000_000


class NotAThumbnail(Exception):
    """The stored picture cannot be made small: unreadable, or far too large."""


def thumbnail(png: bytes, width: int) -> bytes:
    """A JPEG ``width`` pixels wide (never wider than the screenshot), same proportions;
    transparent parts are white."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise NotAThumbnail("too large")
            small: Image.Image = image
            if width < image.width:
                height = max(1, round(image.height * width / image.width))
                small = image.resize((width, height), Image.Resampling.LANCZOS)
            rgba = small.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise NotAThumbnail(str(error)) from error
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    out = io.BytesIO()
    flat.save(out, "JPEG", quality=QUALITY, optimize=True)
    return out.getvalue()
