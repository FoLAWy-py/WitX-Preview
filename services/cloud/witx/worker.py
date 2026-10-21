"""Worker process: claims ready runs and executes one step at a time for each, and
enforces deletion and retention in the background."""

import asyncio
import contextlib
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from uuid import UUID

from witx_contracts import logs

from witx.domain import rules
from witx.harness import relay
from witx.harness.loop import Harness
from witx.models import ModelClient
from witx.objects import ObjectStore
from witx.services import retention, schedules, side_chat, titles
from witx.store import runs
from witx.store.db import Database

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        db: Database,
        harness: Harness,
        *,
        concurrency: int = 4,
        # A worker that dies holds its runs this long at most; a live one renews every
        # third of it. Expiring early is safe: writes are fenced by the claim's epoch.
        lease_seconds: int = 60,
        poll_seconds: float = 0.5,
    ) -> None:
        self.db = db
        self.harness = harness
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.owner = f"{socket.gethostname()}:{os.getpid()}"

    async def claim(self, limit: int) -> list[tuple[UUID, UUID, int]]:
        async with self.db.system() as conn:
            return await runs.claim(conn, self.owner, self.lease_seconds, limit)

    async def execute(self, run_id: UUID, tenant_id: UUID, epoch: int) -> None:
        """Runs one step while keeping the claim alive."""

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(self.lease_seconds / 3)
                try:
                    async with self.db.tenant(tenant_id) as conn:
                        if not await runs.heartbeat(conn, run_id, epoch, self.lease_seconds):
                            return
                except Exception as error:
                    # Keep trying: if the lease runs out meanwhile, another worker takes
                    # the run, and this one's late writes are fenced by the epoch.
                    log.warning("could not renew the claim on run %s: %s", run_id, error)

        heartbeat = asyncio.create_task(keep_alive())
        try:
            with logs.log_context(tenant_id=tenant_id, run_id=run_id):
                await self.harness.step(run_id, tenant_id, epoch)
        except Exception:
            # The claim expires and another attempt starts from stored state.
            log.exception("step failed for run %s", run_id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def run_once(self) -> int:
        """Claims and executes whatever is ready right now, side turns included (used by
        tests and tools)."""
        claimed = await self.claim(self.concurrency)
        await asyncio.gather(*(self.execute(*c) for c in claimed))
        return len(claimed) + await relay.take_open(self.harness)

    async def run_forever(self, stop: asyncio.Event) -> None:
        active: set[asyncio.Task[None]] = set()
        backoff = 0.0
        while not stop.is_set():
            free = self.concurrency - len(active)
            try:
                claimed = await self.claim(free) if free > 0 else []
                backoff = 0.0
            except Exception as error:
                # The database is unreachable (or a claim failed): keep the runs in hand
                # going and try again; runs this worker held go to others when their
                # lease runs out.
                backoff = min(max(backoff * 2, 1.0), 30.0)
                log.warning("could not claim runs: %s; retrying in %.0fs", error, backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), backoff)
                continue
            for claim in claimed:
                task = asyncio.create_task(self.execute(*claim))
                active.add(task)
                task.add_done_callback(active.discard)
            if not claimed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.poll_seconds)
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def maintain_forever(
    db: Database,
    objects: ObjectStore,
    stop: asyncio.Event,
    *,
    screenshot_retention: timedelta,
    audit_retention: timedelta,
    every_seconds: float = 60,
) -> None:
    """Purges deleted tasks and expired data once a minute. Several workers may do this
    at once: each piece of work is locked by whoever takes it first."""
    while not stop.is_set():
        try:
            done = await retention.purge(
                db,
                objects,
                now=datetime.now(UTC),
                screenshot_retention=screenshot_retention,
                audit_retention=audit_retention,
            )
            if done.tasks or done.screenshots or done.tombstones:
                log.info(
                    "purged %d deleted tasks, %d expired screenshots, %d tombstones",
                    done.tasks,
                    done.screenshots,
                    done.tombstones,
                )
        except Exception:
            log.exception("retention pass failed; retrying later")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_seconds)


async def sweep_forever(
    db: Database,
    objects: ObjectStore,
    stop: asyncio.Event,
    *,
    every: timedelta = timedelta(days=1),
    retry: timedelta = timedelta(hours=1),
) -> None:
    """Sweeps screenshot objects no observation holds when the worker starts, then once
    a day; an hour after a sweep that could not finish. One worker at a time, since it
    lists every screenshot. Its own loop, so a long or failing sweep holds up nothing
    else: nothing it raises leaves this function."""
    while not stop.is_set():
        wait = every
        try:
            async with db.only_one("screenshot-sweep") as mine:
                if mine:
                    swept = await retention.sweep_screenshots(db, objects)
                    if swept.failed or swept.error:
                        wait = retry
        except Exception:
            log.exception("screenshot sweep failed; retrying later")
            wait = retry
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), wait.total_seconds())


async def schedule_forever(db: Database, stop: asyncio.Event) -> None:
    """Starts scheduled tasks when their moment comes: it sleeps until the soonest
    schedule is due, and looks at least every SCHEDULE_POLL. Several workers may run
    this at once: each due schedule is locked by whoever takes it first."""
    while not stop.is_set():
        soonest = None
        try:
            done = await schedules.run_due(db, datetime.now(UTC))
            soonest = done.soonest
            if done.started:
                log.info("started %d scheduled tasks", done.started)
        except Exception:
            log.exception("schedule pass failed; retrying later")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), rules.schedule_wait(soonest, datetime.now(UTC)))


async def title_forever(
    db: Database,
    model: ModelClient,
    model_name: str,
    stop: asyncio.Event,
    *,
    every_seconds: float = 5,
) -> None:
    """Gives new tasks a short title for the task list."""
    while not stop.is_set():
        try:
            await titles.title_tasks(db, model, model_name)
        except Exception:
            log.exception("titling failed; retrying later")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_seconds)


async def relay_forever(harness: Harness, stop: asyncio.Event, *, every_seconds: float = 1) -> None:
    """Takes the main agents' side turns while their sub-agents work."""
    while not stop.is_set():
        try:
            await relay.take_open(harness)
        except Exception:
            log.exception("side turns failed; retrying later")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_seconds)


async def side_chat_forever(
    db: Database,
    model: ModelClient,
    objects: ObjectStore,
    stop: asyncio.Event,
    *,
    every_seconds: float = 1,
) -> None:
    """Answers the questions people ask about tasks in side chats."""
    while not stop.is_set():
        try:
            await side_chat.answer_open(db, model, objects)
        except Exception:
            log.exception("side chat answering failed; retrying later")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_seconds)
