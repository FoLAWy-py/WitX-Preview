"""Idempotency keys: the response a write request got, for retries with the same key."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.store.db import execute, fetch_one, jsonb


@dataclass(frozen=True)
class StoredResponse:
    request: str
    digest: str
    response: Any


async def lock(conn: AsyncConnection, tenant_id: UUID, key: str) -> None:
    """Serialises requests with the same key until the end of the transaction."""
    await execute(
        conn, "SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))", k=f"{tenant_id}:{key}"
    )


async def get(conn: AsyncConnection, key: str, *, since: datetime) -> StoredResponse | None:
    row = await fetch_one(
        conn,
        """SELECT request, digest, response FROM idempotency_keys
            WHERE key = :k AND created_at >= :since""",
        k=key,
        since=since,
    )
    if row is None:
        return None
    return StoredResponse(row["request"], row["digest"], row["response"])


async def put(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    key: str,
    user_id: UUID,
    request: str,
    digest: str,
    response: Any,
) -> None:
    """Stores the response; an expired entry with the same key is replaced."""
    await execute(
        conn,
        """INSERT INTO idempotency_keys
                  (tenant_id, key, user_id, request, digest, response)
           VALUES (:t, :k, :u, :r, :d, CAST(:resp AS jsonb))
           ON CONFLICT (tenant_id, key) DO UPDATE
              SET user_id = EXCLUDED.user_id, request = EXCLUDED.request,
                  digest = EXCLUDED.digest, response = EXCLUDED.response, created_at = now()""",
        t=tenant_id,
        k=key,
        u=user_id,
        r=request,
        d=digest,
        resp=jsonb(response),
    )
