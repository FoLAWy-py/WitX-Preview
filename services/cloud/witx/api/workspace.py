"""Passes the workspace notices (``witx.store.notices``) to the pages connected to this
API process, each page its tenant's.

One connection of its own listens. A page subscribes before it loads anything, so a
change made in between still reaches it. Notices are not kept: when the listening
connection had to be made again, or a page fell behind, the page is told to load
everything ("all").
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from witx.store.notices import CHANNEL

log = logging.getLogger(__name__)

# How many notices a page may have waiting before it is told to load everything instead.
WAITING_MAX = 200
RETRY_S = (0.5, 1, 2, 5, 10)
# How long a page that subscribes waits for the listening connection.
LISTEN_WAIT_S = 5


@dataclass
class Notice:
    kind: str  # one of notices.KINDS, or "all": load everything
    task_id: str | None = None
    type: str | None = None


@dataclass(eq=False)
class Page:
    tenant_id: UUID
    notices: asyncio.Queue[Notice] = field(default_factory=lambda: asyncio.Queue(WAITING_MAX))

    def tell(self, notice: Notice) -> None:
        try:
            self.notices.put_nowait(notice)
        except asyncio.QueueFull:
            # Behind: what it waits for is replaced by one "load everything".
            while not self.notices.empty():
                self.notices.get_nowait()
            self.notices.put_nowait(Notice("all"))


class Workspace:
    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.pages: set[Page] = set()
        self.listening: asyncio.Task[None] | None = None
        self.listens = asyncio.Event()

    async def subscribe(self, tenant_id: UUID) -> Page:
        """A page of the tenant, told of every change committed after this returns."""
        page = Page(tenant_id)
        self.pages.add(page)
        if self.listening is None or self.listening.done():
            self.listening = asyncio.create_task(self._listen())
        with contextlib.suppress(TimeoutError):
            # Not listening in time: the page is told to load everything once it is.
            await asyncio.wait_for(self.listens.wait(), LISTEN_WAIT_S)
        return page

    def unsubscribe(self, page: Page) -> None:
        self.pages.discard(page)

    async def close(self) -> None:
        if self.listening is not None:
            self.listening.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.listening

    async def _listen(self) -> None:
        tries = 0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.listens.set()
                    if tries:  # notices may have gone by while it was not listening
                        for page in list(self.pages):
                            page.tell(Notice("all"))
                    tries = 0
                    async for message in conn.notifies():
                        self._pass_on(message.payload)
            except (psycopg.Error, OSError) as error:
                log.warning("workspace notices: listening failed", extra={"error": str(error)})
            self.listens.clear()
            tries += 1
            await asyncio.sleep(RETRY_S[min(tries, len(RETRY_S)) - 1])

    def _pass_on(self, payload: str) -> None:
        try:
            data: dict[str, Any] = json.loads(payload)
            tenant = UUID(str(data["tenant"]))
        except (ValueError, KeyError, TypeError):
            return  # no tenant: a change outside any tenant's pages
        notice = Notice(str(data["kind"]), data.get("task"), data.get("type"))
        for page in list(self.pages):
            if page.tenant_id == tenant:
                page.tell(notice)
