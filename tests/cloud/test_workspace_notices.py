"""A workspace's pages hear of its changes as they commit, and only of their own."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from witx.api.workspace import Notice, Page, Workspace
from witx.domain.model import PermissionMode
from witx.services import lifecycle
from witx.store import tasks
from witx.store.db import Database, fetch

from tests.cloud.support import provision
from tests.conftest import Databases


@pytest.fixture
async def workspace(databases: Databases) -> AsyncIterator[Workspace]:
    listening = Workspace(databases.app_url)
    yield listening
    await listening.close()


async def heard(page: Page, wait: float = 2) -> Notice | None:
    try:
        return await asyncio.wait_for(page.notices.get(), wait)
    except TimeoutError:
        return None


async def test_a_change_reaches_the_pages_of_its_tenant_and_no_other(
    workspace: Workspace, db: Database, admin_db: Database
) -> None:
    acme, other = await provision(admin_db, "acme"), await provision(admin_db, "other")
    mine, theirs = await workspace.subscribe(acme.id), await workspace.subscribe(other.id)

    async with db.tenant(acme.id) as conn:
        await tasks.create_group(conn, tenant_id=acme.id, user_id=acme.user_id, name="Shopping")

    assert await heard(mine) == Notice("groups")
    assert await heard(theirs, wait=0.3) is None


async def test_a_task_s_event_reaches_the_page_with_the_task_and_what_happened(
    workspace: Workspace, db: Database, admin_db: Database
) -> None:
    acme = await provision(admin_db, "acme")
    page = await workspace.subscribe(acme.id)

    async with db.tenant(acme.id) as conn:
        task = await lifecycle.create_task(
            conn,
            tenant_id=acme.id,
            user_id=acme.user_id,
            request="Check the weather",
            constraints=[],
            device_ids=[],
            model="test-model",
            permission_mode=PermissionMode.AUTO,
            timezone="UTC",
        )

    notice = await heard(page)
    assert notice == Notice("task", str(task.id), "task.created")


async def test_a_change_rolled_back_is_never_heard(
    workspace: Workspace, db: Database, admin_db: Database
) -> None:
    acme = await provision(admin_db, "acme")
    page = await workspace.subscribe(acme.id)

    with pytest.raises(RuntimeError):
        async with db.tenant(acme.id) as conn:
            await tasks.create_group(conn, tenant_id=acme.id, user_id=acme.user_id, name="Gone")
            raise RuntimeError("the request failed after it")

    assert await heard(page, wait=0.5) is None


async def test_pages_load_everything_when_listening_had_to_start_again(
    workspace: Workspace, admin_db: Database
) -> None:
    acme = await provision(admin_db, "acme")
    page = await workspace.subscribe(acme.id)

    async with admin_db.system() as conn:  # the listening connection is cut
        (cut,) = await fetch(
            conn,
            "SELECT pg_terminate_backend(pid) AS ok FROM pg_stat_activity"
            " WHERE query LIKE 'LISTEN witx_workspace%'",
        )
    assert cut["ok"]

    # Whatever went by meanwhile is not known: the page loads everything.
    assert await heard(page, wait=5) == Notice("all")
