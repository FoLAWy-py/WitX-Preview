"""Database access: connections, tenant scoping and migrations."""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


def async_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Database:
    """Owns the connection pool. All writes happen in short transactions."""

    def __init__(self, url: str, *, pool_size: int = 10) -> None:
        self.engine: AsyncEngine = create_async_engine(
            async_url(url), pool_size=pool_size, pool_pre_ping=True
        )

    @asynccontextmanager
    async def tenant(self, tenant_id: UUID) -> AsyncIterator[AsyncConnection]:
        """A transaction that row-level security limits to one tenant."""
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('witx.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            yield conn

    @asynccontextmanager
    async def system(self) -> AsyncIterator[AsyncConnection]:
        """A transaction without a tenant. Tenant tables appear empty here."""
        async with self.engine.begin() as conn:
            yield conn

    @asynccontextmanager
    async def only_one(self, name: str) -> AsyncIterator[bool]:
        """Whether this process may do the work named ``name`` now: true for one process
        at a time across all workers (a PostgreSQL advisory lock held on its own
        connection until the block ends); the others skip their turn."""
        async with self.engine.connect() as conn:
            got = bool(
                (
                    await conn.execute(
                        text("SELECT pg_try_advisory_lock(hashtextextended(:n, 0))"), {"n": name}
                    )
                ).scalar()
            )
            await conn.commit()
            try:
                yield got
            finally:
                if got:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:n, 0))"), {"n": name}
                    )
                    await conn.commit()

    async def close(self) -> None:
        await self.engine.dispose()


def jsonb(value: object) -> str | None:
    """Parameters for `CAST(:x AS jsonb)` columns."""
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


async def fetch(conn: AsyncConnection, sql: str, **params: Any) -> Sequence[RowMapping]:
    return (await conn.execute(text(sql), params)).mappings().all()


async def fetch_one(conn: AsyncConnection, sql: str, **params: Any) -> RowMapping | None:
    return (await conn.execute(text(sql), params)).mappings().first()


async def execute(conn: AsyncConnection, sql: str, **params: Any) -> int:
    return (await conn.execute(text(sql), params)).rowcount


def as_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------- migrations


def _migrations() -> list[tuple[str, str]]:
    folder = resources.files("witx.store") / "migrations"
    files = sorted((p for p in folder.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name.removesuffix(".sql"), p.read_text(encoding="utf-8")) for p in files]


async def migrate(url: str) -> list[str]:
    """Applies pending migrations in order, under an advisory lock. Returns what ran.

    Run this with the role that owns the schema, never with the application role.
    """
    engine = create_async_engine(async_url(url))
    applied: list[str] = []
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(4711)"))
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
            )
            done: set[str] = set(
                (await conn.execute(text("SELECT version FROM schema_migrations"))).scalars()
            )
            raw = (await conn.get_raw_connection()).driver_connection
            assert raw is not None
            for version, sql in _migrations():
                if version in done:
                    continue
                # Sent as one script without parameters (same connection and transaction).
                await raw.execute(sql.encode())
                await conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": version}
                )
                applied.append(version)
    finally:
        await engine.dispose()
    return applied
