"""A phone's own screen broadcast: the MJPEG stream the Appium drivers serve on a local
port (WebDriverAgent on iOS, the UiAutomator2 server on Android). A person who takes a
phone over sees it move at the stream's rate instead of one screenshot at a time.

Only the newest picture is kept. The stream is opened when a picture is asked for and
closed when nobody has asked for a while, so a phone that is not being watched is not
encoding video. Any trouble (nothing listening, a stalled or malformed stream) ends the
stream; callers then fall back to screenshots, and it is not tried again for a while."""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

# Nobody asked for a picture for this long: stop reading.
IDLE = 5.0
# Pictures come at several per second even when the screen is still; a stream that says
# nothing for this long is broken.
STALL = 5.0
# After a failure, screenshots are used for this long before the stream is tried again.
RETRY_AFTER = 15.0
# A picture older than this is not shown as if it were current.
FRESH = 3.0
MAX_HEAD = 4096
MAX_PICTURE = 8_000_000

LENGTH = re.compile(rb"content-length:\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class StreamFrame:
    number: int  # counts up with every picture the stream delivers
    jpeg: bytes


async def pictures(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """The JPEGs in a multipart/x-mixed-replace body. Each part states its length, which
    is what tells where it ends: a JPEG can hold byte pairs that look like its markers."""
    buffer = bytearray()
    async for chunk in chunks:
        buffer += chunk
        while True:
            while buffer[:2] == b"\r\n":
                del buffer[:2]  # what follows a picture, before the next boundary
            end_of_head = buffer.find(b"\r\n\r\n")
            if end_of_head < 0:
                if len(buffer) > MAX_HEAD:
                    raise ValueError("no part header")
                break
            found = LENGTH.search(bytes(buffer[:end_of_head]))
            if found is None:
                raise ValueError("a part without a length")
            length = int(found[1])
            if length > MAX_PICTURE:
                raise ValueError("a part too large")
            start = end_of_head + 4
            if len(buffer) < start + length:
                break
            jpeg = bytes(buffer[start : start + length])
            if jpeg[:2] != b"\xff\xd8":
                raise ValueError("a part that is not a JPEG")
            del buffer[: start + length]
            yield jpeg


class MjpegStream:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = url
        self._transport = transport
        self._reader: asyncio.Task[None] | None = None
        self._latest: StreamFrame | None = None
        self._count = 0  # numbers go on counting when the stream is opened again
        self._arrived_at = 0.0
        self._asked_at = 0.0
        self._failed_at: float | None = None
        self._news = asyncio.Condition()

    async def next(self, after: int, wait: float) -> StreamFrame | None:
        """The newest picture once it is newer than ``after`` (a stream that does not
        deliver one within ``wait`` gives the one it has, if that is recent). None when
        there is no stream to show: it could not be opened, or it broke."""
        loop = asyncio.get_running_loop()
        self._asked_at = loop.time()
        if self._reader is None:
            if self._failed_at is not None and loop.time() - self._failed_at < RETRY_AFTER:
                return None
            self._failed_at = None
            self._reader = asyncio.create_task(self._read())
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait):
                async with self._news:
                    await self._news.wait_for(
                        lambda: (
                            self._reader is None
                            or (self._latest is not None and self._latest.number > after)
                        )
                    )
        latest = self._latest
        if latest is None or loop.time() - self._arrived_at > FRESH:
            return None
        return latest

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    async def _read(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    trust_env=False,  # the stream is on this computer: no proxy in between
                    timeout=httpx.Timeout(STALL),
                ) as client,
                client.stream("GET", self._url) as response,
            ):
                response.raise_for_status()
                async for jpeg in pictures(response.aiter_bytes()):
                    self._count += 1
                    self._arrived_at = loop.time()
                    async with self._news:
                        self._latest = StreamFrame(self._count, jpeg)
                        self._news.notify_all()
                    if loop.time() - self._asked_at > IDLE:
                        return  # nobody is looking
        except (httpx.HTTPError, ValueError):
            self._failed_at = loop.time()
        finally:
            self._reader = None
            self._latest = None
            async with self._news:
                self._news.notify_all()
