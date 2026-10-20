"""The phone's own screen broadcast: parsing the stream and keeping the newest picture."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from witx_gateway import mjpeg
from witx_gateway.mjpeg import MjpegStream, pictures

JPEG_A = b"\xff\xd8 picture a with \xff\xd9 inside \xff\xd9"
JPEG_B = b"\xff\xd8 picture b"


def part(jpeg: bytes) -> bytes:
    head = b"--BoundaryString\r\nContent-type: image/jpg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)
    return head + jpeg + b"\r\n\r\n"  # the Android server ends each picture with a blank line


async def chunked(body: bytes, size: int) -> AsyncIterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


async def test_pictures_are_cut_out_by_their_stated_length_however_the_body_arrives() -> None:
    body = part(JPEG_A) + part(JPEG_B)
    for size in (1, 7, len(body)):
        assert [jpeg async for jpeg in pictures(chunked(body, size))] == [JPEG_A, JPEG_B]


async def test_a_part_that_states_no_length_is_refused() -> None:
    body = b"--BoundaryString\r\nContent-type: image/jpg\r\n\r\n" + JPEG_A
    with pytest.raises(ValueError):
        [jpeg async for jpeg in pictures(chunked(body, 64))]


def broadcast(*, endless: bool = True, status: int = 200) -> httpx.MockTransport:
    async def body() -> AsyncIterator[bytes]:
        number = 0
        while True:
            yield part(JPEG_A if number % 2 == 0 else JPEG_B)
            number += 1
            if not endless and number == 2:
                return
            await asyncio.sleep(0.01)

    return httpx.MockTransport(lambda request: httpx.Response(status, content=body()))


async def test_the_stream_gives_the_newest_picture_once_it_is_newer_than_the_last_one_sent() -> (
    None
):
    stream = MjpegStream("http://127.0.0.1:9200", transport=broadcast())
    first = await stream.next(after=0, wait=1)
    assert first is not None
    second = await stream.next(after=first.number, wait=1)
    assert second is not None and second.number > first.number
    await stream.close()


async def test_a_stream_that_cannot_be_opened_is_not_tried_again_for_a_while() -> None:
    opened: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        opened.append(str(request.url))
        return httpx.Response(503)

    stream = MjpegStream("http://127.0.0.1:9200", transport=httpx.MockTransport(refuse))
    assert await stream.next(after=0, wait=1) is None
    assert await stream.next(after=0, wait=1) is None
    assert len(opened) == 1
    assert mjpeg.RETRY_AFTER > 1  # long enough that the second call really was inside it


async def test_a_stream_that_ended_is_opened_again_when_a_picture_is_asked_for() -> None:
    stream = MjpegStream("http://127.0.0.1:9200", transport=broadcast(endless=False))
    first = await stream.next(after=0, wait=1)
    assert first is not None
    await asyncio.sleep(0.1)  # the stream has ended
    again = await stream.next(after=first.number, wait=1)
    assert again is not None and again.number > first.number  # numbers go on counting
    await stream.close()
