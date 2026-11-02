"""Files a person gives a task, and the links a phone downloads them by.

A file is uploaded on its own, then attached to a task with the message it came with, so
the agent reads the message and its files at once. The phone fetches a file by a link the
cloud signs with the tenant's key: it names one file and works for minutes, without a
sign-in, so nothing long-lived sits on the phone. The link is made here, from a file the
agent names; it never passes through the model.
"""

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.model import TaskFile
from witx.domain.rules import Rejected
from witx.objects import ObjectStore, file_key
from witx.store import files

# The part of a file a download sends at a time: memory stays at this, whatever the size.
CHUNK = 1024 * 1024


async def upload(
    conn: AsyncConnection,
    objects: ObjectStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    name: str,
    content_type: str,
    data: bytes,
) -> TaskFile:
    """Keeps the bytes and their row in one go: the object is written before the row
    commits, so a row always has its bytes."""
    rules.check_file_size(len(data))
    kept = await files.add(
        conn,
        id=uuid4(),
        tenant_id=tenant_id,
        name=rules.file_name(name),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type or "application/octet-stream",
        created_by=user_id,
    )
    await objects.put(file_key(tenant_id, kept.id), data, kept.content_type)
    return kept


async def attach(
    conn: AsyncConnection,
    file_ids: Sequence[UUID],
    *,
    task_id: UUID,
    input_seq: int | None,
    user_id: UUID,
) -> None:
    """Attaches the person's own uploads, each once; refuses any other ID."""
    ids = list(dict.fromkeys(file_ids))
    if len(ids) > rules.MAX_FILES_PER_MESSAGE:
        raise Rejected("invalid_arguments", f"at most {rules.MAX_FILES_PER_MESSAGE} files")
    if ids and await files.attach(
        conn, ids, task_id=task_id, input_seq=input_seq, user_id=user_id
    ) != len(ids):
        raise Rejected("unknown_file", "upload each file first; a file comes with one message")


async def link_token(conn: AsyncConnection, tenant_id: UUID, file_id: UUID, now: datetime) -> str:
    key = await files.link_key(conn, tenant_id)
    assert key is not None
    return rules.link_token(key, tenant_id, file_id, now + rules.LINK_TTL)


async def delivery(
    conn: AsyncConnection,
    task_id: UUID,
    tenant_id: UUID,
    payload: dict[str, Any],
    *,
    now: datetime,
    public_url: str,
) -> dict[str, Any]:
    """A ``receive_file`` input as the phone needs it: the agent named a file of this task,
    and the cloud adds a fresh link to it and its SHA-256. A file of another task, or one
    not sent with a message yet, is refused."""
    try:
        file = await files.get(conn, UUID(str(payload.get("file_id"))))
    except ValueError:
        file = None
    if file is None or file.task_id != task_id:
        raise Rejected("unknown_file", "send a file the user gave this task, by its file_id")
    token = await link_token(conn, tenant_id, file.id, now)
    return {**payload, "url": f"{public_url}/api/v1/downloads/{token}", "sha256": file.sha256}


async def chunks(
    objects: ObjectStore, tenant_id: UUID, file: TaskFile, start: int, end: int
) -> AsyncIterator[bytes]:
    """Bytes ``start`` to ``end`` of the file, a chunk at a time."""
    key = file_key(tenant_id, file.id)
    at = start
    while at <= end:
        last = min(end, at + CHUNK - 1)
        yield await objects.get_range(key, at, last)
        at = last + 1
