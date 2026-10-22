"""Notices that something a workspace's pages show has changed.

A notice goes out with ``pg_notify`` when the transaction that made the change commits,
from the API and the worker alike, and carries no content: the pages that hear it load
what changed. The API listens (``witx.api.workspace``) and passes each notice to the
pages of its tenant.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.store.db import execute

CHANNEL = "witx_workspace"

# What a notice says changed: a task (its row, its place in the lists, whether it waits
# for someone), the groups, the devices and their pools, or the schedules.
KINDS = ("task", "groups", "devices", "schedules")

# The notice as JSON, built in SQL. Its tenant is the transaction's, unless given.
_NOTICE = """json_build_object(
    'tenant', COALESCE(CAST(:tenant AS text), current_setting('witx.tenant_id', true)),
    'kind', CAST(:kind AS text), 'task', CAST(:task AS text), 'type', CAST(:type AS text))::text"""


async def notify(
    conn: AsyncConnection,
    kind: str,
    *,
    task_id: UUID | None = None,
    type: str | None = None,
    tenant_id: UUID | None = None,
) -> None:
    """Tells the tenant's pages, once this transaction commits, that something of
    ``kind`` changed (a task's event ``type``, for one)."""
    assert kind in KINDS
    await execute(
        conn,
        f"SELECT pg_notify('{CHANNEL}', {_NOTICE})",
        tenant=tenant_id,
        kind=kind,
        task=task_id,
        type=type,
    )
