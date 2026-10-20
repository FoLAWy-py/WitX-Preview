"""Long-term memory : entries kept for a person's own future tasks,
written by them or by an agent on its own."""

from collections import defaultdict
from uuid import UUID, uuid4

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import Memory
from witx.store.db import execute, fetch, fetch_one

_MEMORY = """id, user_id, text, status, source_task_id, created_at, decided_at, kind, app,
             by_agent"""


def _memory(row: RowMapping) -> Memory:
    return Memory(**dict(row))


async def add(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    text: str,
    kind: str = "user",
    app: str | None = None,
    source_task_id: UUID | None = None,
) -> Memory:
    row = await fetch_one(
        conn,
        f"""INSERT INTO user_memories (id, tenant_id, user_id, text, status, source_task_id,
                                       kind, app, by_agent, decided_at)
            VALUES (:id, :t, :u, :text, 'kept', :task, :kind, :app, :by_agent, now())
            RETURNING {_MEMORY}""",
        id=uuid4(),
        t=tenant_id,
        u=user_id,
        text=text,
        task=source_task_id,
        kind=kind,
        app=app,
        by_agent=source_task_id is not None,
    )
    assert row is not None
    return _memory(row)


async def replace_text(conn: AsyncConnection, memory_id: UUID, text: str, task_id: UUID) -> None:
    await execute(
        conn,
        """UPDATE user_memories SET text = :text, source_task_id = :task, by_agent = true,
                  decided_at = now()
            WHERE id = :id""",
        id=memory_id,
        text=text,
        task=task_id,
    )


async def get(conn: AsyncConnection, memory_id: UUID, *, lock: bool = False) -> Memory | None:
    row = await fetch_one(
        conn,
        f"SELECT {_MEMORY} FROM user_memories WHERE id = :id" + (" FOR UPDATE" if lock else ""),
        id=memory_id,
    )
    return _memory(row) if row else None


async def of_user(conn: AsyncConnection, user_id: UUID) -> list[Memory]:
    """A person's kept entries, oldest first."""
    rows = await fetch(
        conn,
        f"""SELECT {_MEMORY} FROM user_memories WHERE user_id = :u AND status = 'kept'
             ORDER BY created_at""",
        u=user_id,
    )
    return [_memory(r) for r in rows]


async def from_task(conn: AsyncConnection, task_id: UUID) -> list[Memory]:
    """What the task's agent kept, oldest first (shown in its conversation)."""
    rows = await fetch(
        conn,
        f"""SELECT {_MEMORY} FROM user_memories WHERE source_task_id = :t AND status = 'kept'
             ORDER BY created_at""",
        t=task_id,
    )
    return [_memory(r) for r in rows]


async def for_task(conn: AsyncConnection, task_id: UUID, *, auto: bool) -> list[Memory]:
    """The entries of the person who started the task; without ``auto``, only those they
    wrote themselves."""
    rows = await fetch(
        conn,
        f"""SELECT {", ".join("m." + c.strip() for c in _MEMORY.split(","))}
              FROM user_memories m JOIN tasks t ON t.created_by = m.user_id
             WHERE t.id = :t AND m.status = 'kept'
               AND (CAST(:auto AS boolean) OR NOT m.by_agent)
             ORDER BY m.created_at""",
        t=task_id,
        auto=auto,
    )
    return [_memory(r) for r in rows]


def by_app(entries: list[Memory]) -> dict[str, list[Memory]]:
    found: dict[str, list[Memory]] = defaultdict(list)
    for entry in entries:
        if entry.app is not None:
            found[entry.app].append(entry)
    return dict(found)


async def delete(conn: AsyncConnection, memory_id: UUID) -> None:
    await execute(conn, "DELETE FROM user_memories WHERE id = :id", id=memory_id)
