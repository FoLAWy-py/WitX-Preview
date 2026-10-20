"""Side chats: questions about a task and their answers."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import SideMessage
from witx.store.db import execute, fetch, fetch_one, jsonb

_MESSAGE = """id, tenant_id, task_id, thread_id, question, answer, error, tries, asked_by,
              created_at, answered_at"""


def _message(row: RowMapping) -> SideMessage:
    return SideMessage(
        id=row["id"],
        tenant_id=row["tenant_id"],
        task_id=row["task_id"],
        thread_id=row["thread_id"],
        question=row["question"],
        asked_by=row["asked_by"],
        created_at=row["created_at"],
        answer=row["answer"],
        error=row["error"],
        tries=row["tries"],
        answered_at=row["answered_at"],
    )


async def insert(
    conn: AsyncConnection,
    *,
    id: UUID,
    tenant_id: UUID,
    task_id: UUID,
    thread_id: UUID,
    question: str,
    asked_by: UUID,
) -> SideMessage:
    row = await fetch_one(
        conn,
        f"""INSERT INTO side_messages (id, tenant_id, task_id, thread_id, question, asked_by)
            VALUES (:id, :tenant, :task, :thread, :question, :by)
            RETURNING {_MESSAGE}""",
        id=id,
        tenant=tenant_id,
        task=task_id,
        thread=thread_id,
        question=question,
        by=asked_by,
    )
    assert row is not None
    return _message(row)


async def for_task(conn: AsyncConnection, task_id: UUID) -> list[SideMessage]:
    rows = await fetch(
        conn,
        f"SELECT {_MESSAGE} FROM side_messages WHERE task_id = :t ORDER BY created_at",
        t=task_id,
    )
    return [_message(r) for r in rows]


async def open_questions(conn: AsyncConnection, limit: int = 20) -> list[SideMessage]:
    """Questions still waiting for an answer, oldest first."""
    rows = await fetch(
        conn,
        f"""SELECT {_MESSAGE} FROM side_messages
             WHERE answer IS NULL AND error IS NULL ORDER BY created_at LIMIT :n""",
        n=limit,
    )
    return [_message(r) for r in rows]


async def set_answer(
    conn: AsyncConnection, message_id: UUID, answer: str, call: dict[str, Any], now: datetime
) -> None:
    await execute(
        conn,
        """UPDATE side_messages SET answer = :a, call = CAST(:c AS jsonb), answered_at = :now,
                  tries = tries + 1
            WHERE id = :id AND answer IS NULL""",
        id=message_id,
        a=answer,
        c=jsonb(call),
        now=now,
    )


async def set_failed(
    conn: AsyncConnection, message_id: UUID, error: str | None, call: dict[str, Any] | None
) -> None:
    """Counts a failed try; ``error`` set: the question gets no answer."""
    await execute(
        conn,
        """UPDATE side_messages SET tries = tries + 1, error = :e,
                  call = COALESCE(CAST(:c AS jsonb), call)
            WHERE id = :id AND answer IS NULL""",
        id=message_id,
        e=error,
        c=jsonb(call) if call is not None else None,
    )


async def delete_thread(conn: AsyncConnection, task_id: UUID, thread_id: UUID) -> None:
    await execute(
        conn,
        "DELETE FROM side_messages WHERE task_id = :t AND thread_id = :th",
        t=task_id,
        th=thread_id,
    )
