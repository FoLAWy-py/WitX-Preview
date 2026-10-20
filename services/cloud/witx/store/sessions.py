"""Browser sessions : an HttpOnly cookie holds a random token; only its hash
is stored, so a database leak does not hand out sessions."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.store.db import execute, fetch_one


@dataclass(frozen=True)
class Session:
    user_id: UUID
    subject: str
    csrf: str
    seen_at: datetime


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create(
    conn: AsyncConnection, *, user_id: UUID, subject: str, now: datetime
) -> tuple[str, str]:
    """A new session. Returns (cookie token, CSRF token)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    await execute(
        conn,
        """INSERT INTO sessions (token_hash, user_id, subject, csrf, created_at, seen_at,
                                 expires_at)
           VALUES (:h, :u, :s, :c, :now, :now, :exp)""",
        h=_hash(token),
        u=user_id,
        s=subject,
        c=csrf,
        now=now,
        exp=now + rules.SESSION_MAX,
    )
    return token, csrf


async def get(conn: AsyncConnection, token: str, now: datetime) -> Session | None:
    """The session for a cookie token, if it is still valid."""
    row = await fetch_one(
        conn,
        """SELECT user_id, subject, csrf, seen_at, expires_at, revoked_at FROM sessions
            WHERE token_hash = :h""",
        h=_hash(token),
    )
    if row is None or not rules.session_valid(
        seen_at=row["seen_at"], expires_at=row["expires_at"], revoked_at=row["revoked_at"], now=now
    ):
        return None
    return Session(row["user_id"], row["subject"], row["csrf"], row["seen_at"])


async def touch(conn: AsyncConnection, token: str, now: datetime) -> None:
    await execute(
        conn, "UPDATE sessions SET seen_at = :now WHERE token_hash = :h", h=_hash(token), now=now
    )


async def revoke(conn: AsyncConnection, token: str, now: datetime) -> None:
    await execute(
        conn,
        "UPDATE sessions SET revoked_at = :now WHERE token_hash = :h AND revoked_at IS NULL",
        h=_hash(token),
        now=now,
    )
