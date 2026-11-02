"""Files a person gives a task: the rows; the bytes are in object storage."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import TaskFile
from witx.store.db import execute, fetch, fetch_one

_FILE = "id, task_id, input_seq, name, size, sha256, content_type, created_by, created_at"


def _file(row: RowMapping) -> TaskFile:
    return TaskFile(**dict(row))


async def add(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    name: str,
    size: int,
    sha256: str,
    content_type: str,
    created_by: UUID,
) -> TaskFile:
    row = await fetch_one(
        conn,
        f"""INSERT INTO files (id, tenant_id, name, size, sha256, content_type, created_by)
            VALUES (:id, :t, :name, :size, :sha, :ct, :by) RETURNING {_FILE}""",
        id=id,
        t=tenant_id,
        name=name,
        size=size,
        sha=sha256,
        ct=content_type,
        by=created_by,
    )
    assert row is not None
    return _file(row)


async def get(conn: AsyncConnection, file_id: UUID) -> TaskFile | None:
    row = await fetch_one(conn, f"SELECT {_FILE} FROM files WHERE id = :id", id=file_id)
    return _file(row) if row else None


async def attach(
    conn: AsyncConnection,
    file_ids: Sequence[UUID],
    *,
    task_id: UUID,
    input_seq: int | None,
    user_id: UUID,
) -> int:
    """Attaches the person's own files that are not attached yet; how many were."""
    return await execute(
        conn,
        """UPDATE files SET task_id = :task, input_seq = :seq
            WHERE id = ANY(:ids) AND task_id IS NULL AND created_by = :u""",
        ids=list(file_ids),
        task=task_id,
        seq=input_seq,
        u=user_id,
    )


async def detach(conn: AsyncConnection, task_id: UUID, input_seq: int) -> None:
    """The files of a withdrawn message are no longer attached."""
    await execute(
        conn,
        "UPDATE files SET task_id = NULL, input_seq = NULL WHERE task_id = :t AND input_seq = :s",
        t=task_id,
        s=input_seq,
    )


async def of_task(conn: AsyncConnection, task_id: UUID) -> list[TaskFile]:
    """In the order they were given."""
    rows = await fetch(
        conn,
        f"SELECT {_FILE} FROM files WHERE task_id = :t ORDER BY created_at, id",
        t=task_id,
    )
    return [_file(r) for r in rows]


async def unattached(conn: AsyncConnection, *, before: datetime, limit: int) -> list[UUID]:
    rows = await fetch(
        conn,
        """SELECT id FROM files WHERE task_id IS NULL AND created_at < :before
            ORDER BY created_at LIMIT :limit""",
        before=before,
        limit=limit,
    )
    return [r["id"] for r in rows]


async def delete(conn: AsyncConnection, file_ids: Sequence[UUID]) -> None:
    await execute(conn, "DELETE FROM files WHERE id = ANY(:ids)", ids=list(file_ids))


async def link_key(conn: AsyncConnection, tenant_id: UUID) -> bytes | None:
    """The key that signs this tenant's download links."""
    row = await fetch_one(conn, "SELECT link_key FROM tenants WHERE id = :t", t=tenant_id)
    return bytes(row["link_key"]) if row else None
